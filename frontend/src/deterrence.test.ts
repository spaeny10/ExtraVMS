import { describe, expect, it } from "vitest";
import { MAX_RULES, type DeterRule, type Deterrence, type MilesightExtras, anyOn, armedText, countdownText, deterButtons, deterRequest, errorStatus,
  hasDeterrence, lastText, milesightChanges, milesightDraft, milesightProblems, newRule, normalizeRule, offerCameraSpeaker, outputNow, ruleProblems,
  rulesDirty, rulesHaveNothing, rulesProblems, secondsChoices, sectionBody, sirenSourceChoices, sirenSourceName, sirenSpeakerTitle, sirenWayText,
  unavailableText, type SirenRoute } from "./deterrence";

const act = (via: "aux" | "relay", target: string, label: string, timed = true, pulse_s: number | null = null) => ({ via, target, label, timed, pulse_s });
const base: Deterrence = {
  camera_id: "cam1", probed: true, available: true, error: null, milesight: false, audio_outputs: 0, default_s: 10, max_s: 120, now: 1000,
  actions: { strobe: null, siren: null, light: null }, relays: [], state: {}, last: null,
};
const caps = { strobe: true, siren: true, light: false };

describe("what the camera list says", () => {
  it("a button only when something can be driven", () => {
    expect(hasDeterrence(null)).toBe(false);
    expect(hasDeterrence(undefined)).toBe(false);
    expect(hasDeterrence({ strobe: false, siren: false, light: false, relays: 0 })).toBe(false);
    expect(hasDeterrence({ strobe: false, siren: true, light: false, relays: 0 })).toBe(true);
    expect(hasDeterrence({ strobe: false, siren: false, light: false, relays: 2 })).toBe(true);
  });
  it("reads the status off an error", () => {
    expect(errorStatus(new Error('409 {"detail":"no siren"}'))).toBe(409);
    expect(errorStatus("503 camera not probed")).toBe(503);
    expect(errorStatus(new TypeError("Failed to fetch"))).toBe(0);
  });
});

describe("the buttons", () => {
  it("strobe, siren, light in that order, only those the camera has; relays no action uses after", () => {
    const d = {
      ...base,
      actions: { strobe: act("relay", "AlarmOut_0", "Strobe on output 1", false, 3), siren: null, light: act("aux", "tt:WhiteLight|On", "White light") },
      relays: [
        { token: "AlarmOut_0", mode: "monostable" as const, delay_s: 3, idle_state: "open", role: "strobe" as const },
        { token: "AlarmOut_1", mode: "bistable" as const, delay_s: 0, idle_state: "open", role: null },
        { token: "AlarmOut_2", mode: "monostable" as const, delay_s: 5, idle_state: null, role: null },
      ],
    };
    const b = deterButtons(d);
    expect(b.map((x) => x.key)).toEqual(["strobe", "light", "relay:AlarmOut_1", "relay:AlarmOut_2"]);
    expect(b[0]).toMatchObject({ action: "strobe", label: "Strobe", icon: "zap", timed: false });
    expect(b[0].title).toContain("pulses 3 s");
    expect(b[1]).toMatchObject({ action: "light", label: "Light", icon: "bulb", timed: true });
    expect(b[2]).toMatchObject({ action: "relay", token: "AlarmOut_1", label: "Output 2", timed: true });
    expect(b[3]).toMatchObject({ token: "AlarmOut_2", timed: false });
    expect(b[3].title).toContain("pulses 5 s");
  });
  it("a relay an action already drives is not listed twice", () => {
    const d = { ...base, actions: { ...base.actions, siren: act("relay", "R1", "Siren") }, relays: [{ token: "R1", mode: "bistable" as const, delay_s: 0, idle_state: null, role: null }] };
    expect(deterButtons(d).map((x) => x.key)).toEqual(["siren"]);
  });
  it("says why there is nothing", () => {
    expect(unavailableText({ ...base, probed: false })).toMatch(/hasn't been asked/);
    expect(unavailableText({ ...base, available: false, error: "ONVIF timeout" })).toBe("Not available: ONVIF timeout");
    expect(unavailableText(base)).toMatch(/no strobe, siren, light/);
    expect(unavailableText({ ...base, actions: { ...base.actions, siren: act("aux", "x", "Siren") } })).toBeNull();
  });
});

describe("countdowns", () => {
  const on = (remaining_s: number | null) => ({ on: true, until: remaining_s == null ? null : 1010, remaining_s, by: "op", started_at: 1000 });
  it("counts down from the read, never below zero; off once it runs out", () => {
    expect(outputNow(on(10), 0)).toEqual({ on: true, left: 10 });
    expect(outputNow(on(10), 3.5)).toEqual({ on: true, left: 6.5 });
    expect(outputNow(on(10), 12)).toEqual({ on: false, left: 0 });
    expect(outputNow(on(10), -2)).toEqual({ on: true, left: 10 });
  });
  it("an output with no end stays on; an idle one is off", () => {
    expect(outputNow(on(null), 500)).toEqual({ on: true, left: null });
    expect(outputNow({ ...on(5), on: false }, 0)).toEqual({ on: false, left: null });
    expect(outputNow(undefined, 0)).toEqual({ on: false, left: null });
  });
  it("anything on", () => {
    expect(anyOn(null, 0)).toBe(false);
    expect(anyOn({ state: { siren: on(4) } }, 1)).toBe(true);
    expect(anyOn({ state: { siren: on(4) } }, 5)).toBe(false);
  });
  it("as text", () => {
    expect(countdownText(null)).toBe("on");
    expect(countdownText(7.2)).toBe("8 s");
    expect(countdownText(0.1)).toBe("1 s");
    expect(countdownText(65)).toBe("1:05");
    expect(countdownText(120)).toBe("2:00");
  });
});

describe("what a tap sends", () => {
  const siren = { action: "siren" as const, timed: true };
  it("on for the picked seconds; off when it runs", () => {
    expect(deterRequest(siren, false, 30)).toEqual({ action: "siren", on: true, seconds: 30 });
    expect(deterRequest(siren, true, 30)).toEqual({ action: "siren", on: false });
  });
  it("seconds clamped to the server's range; none for an output that times itself", () => {
    expect(deterRequest(siren, false, 500)).toEqual({ action: "siren", on: true, seconds: 120 });
    expect(deterRequest(siren, false, 0)).toEqual({ action: "siren", on: true, seconds: 1 });
    expect(deterRequest({ action: "relay", token: "R2", timed: false }, false, 30)).toEqual({ action: "relay", token: "R2", on: true });
  });
  it("the seconds picker stays within the camera's maximum and has its default", () => {
    expect(secondsChoices(120, 10)).toEqual([5, 10, 30, 60, 120]);
    expect(secondsChoices(45, 15)).toEqual([5, 10, 15, 30]);
  });
});

describe("the last action", () => {
  it("one line: what, who, a failure's reason", () => {
    expect(lastText(null)).toBe("");
    expect(lastText({ ts: 1, action: "siren", on: true, seconds: 10, by: "op@x", source: "manual", result: "ok", error: null })).toBe("Siren on for 10 s · op@x");
    expect(lastText({ ts: 1, action: "strobe", on: true, seconds: 10, by: "Yard at night", source: "rule", result: "ok", error: null }))
      .toBe("Strobe on for 10 s · rule Yard at night");
    expect(lastText({ ts: 1, action: "stop", on: false, seconds: null, by: "op@x", source: "manual", result: "failed", error: "camera refused" }))
      .toBe("Everything off · op@x · failed: camera refused");
    const buttons = deterButtons({ ...base, relays: [{ token: "R1", mode: "bistable", delay_s: 0, idle_state: null, role: null }] });
    expect(lastText({ ts: 1, action: "relay:R1", on: false, seconds: null, by: "op", source: "manual", result: "ok", error: null }, buttons)).toBe("Output 1 off · op");
  });
});

describe("automatic rules", () => {
  const rule = (p: Partial<DeterRule> = {}): DeterRule => ({ ...newRule(), actions: ["siren"], ...p });
  it("a new rule: on, people, only while armed, no outputs yet, 10 s, 2 min cooldown; a free name", () => {
    expect(newRule([{ name: "Rule 1" }, { name: "Rule 3" }])).toEqual({ id: "", name: "Rule 4", enabled: true, labels: ["person"], zone: null, when: "armed",
      actions: [], seconds: 10, speaker_id: null, message_id: null, cooldown_s: 120 });
    expect(newRule([{ name: "Rule 2" }]).name).toBe("Rule 3");
  });
  it("what keeps a rule from saving", () => {
    expect(ruleProblems(rule(), caps)).toEqual([]);
    expect(ruleProblems(rule({ actions: [] }), caps)).toEqual(["pick an output or a talk-down message"]);
    expect(ruleProblems(rule({ labels: [] }), caps)).toEqual(["pick people, vehicles or both"]);
    expect(ruleProblems(rule({ actions: ["light"] }), caps)).toEqual(["this camera has no light"]);
    expect(ruleProblems(rule({ speaker_id: "spk1" }), caps)).toEqual(["pick both a speaker and a message, or neither"]);
    expect(ruleProblems(rule({ actions: [], speaker_id: "spk1", message_id: 2 }), caps)).toEqual([]);
    expect(ruleProblems(rule({ seconds: 0, cooldown_s: 4000 }), caps)).toEqual(["seconds must be 1–120", "cooldown must be 0–3600 s"]);
    expect(ruleProblems(rule({ seconds: NaN }), caps)).toEqual(["seconds must be 1–120"]);
  });
  it("names each problem, and caps the count", () => {
    const many = Array.from({ length: MAX_RULES + 1 }, (_, i) => rule({ name: `R${i}` }));
    expect(rulesProblems([rule({ name: "Gate", labels: [] }), rule({ name: " " , actions: [] })], caps))
      .toEqual(["Gate: pick people, vehicles or both", "Rule 2: pick an output or a talk-down message"]);
    expect(rulesProblems(many, caps)).toEqual([`at most ${MAX_RULES} rules per camera`]);
  });
  it("normalized for the PUT: trimmed, clamped, ordered, a message only with a speaker", () => {
    expect(normalizeRule({ ...rule(), name: "  Gate ", labels: ["vehicle", "person", "person"], actions: ["light", "strobe"], zone: "",
      seconds: 500, cooldown_s: -5, message_id: 3 })).toEqual({ id: "", name: "Gate", enabled: true, labels: ["person", "vehicle"], zone: null, when: "armed",
      actions: ["strobe", "light"], seconds: 120, speaker_id: null, message_id: null, cooldown_s: 0 });
    expect(normalizeRule({ ...rule(), name: "", seconds: NaN }, 2)).toMatchObject({ name: "Rule 3", seconds: 10 });
    expect(normalizeRule({ ...rule(), speaker_id: "spk1", message_id: 0 })).toMatchObject({ speaker_id: "spk1", message_id: 0 });
  });
  it("unsaved changes compare what would be sent", () => {
    const saved = [rule({ name: "Gate" })];
    expect(rulesDirty(saved, [{ ...saved[0], name: "Gate " }])).toBe(false);
    expect(rulesDirty(saved, [{ ...saved[0], seconds: 30 }])).toBe(true);
    expect(rulesDirty(saved, [])).toBe(true);
  });
  it("the armed line", () => {
    expect(armedText({ known: true, armed: true, reason: null })).toEqual({ text: "Site armed now.", tone: "ok" });
    expect(armedText({ known: true, armed: false, reason: "outside SOC hours" })).toEqual({ text: "Site not armed now (outside SOC hours).", tone: "idle" });
    expect(armedText({ known: false, armed: null, reason: null }).text).toMatch(/^No SOC schedule from the hub: rules set to "only while armed" won't fire/);
    expect(armedText({ known: false, armed: null, reason: null }).tone).toBe("warn");
  });
  it("nothing for a rule to do without outputs or a linked speaker", () => {
    expect(rulesHaveNothing({ caps: { strobe: false, siren: false, light: false }, speakers: [] })).toBe(true);
    expect(rulesHaveNothing({ caps: { strobe: false, siren: false, light: false }, speakers: [{ id: "spk1", name: "Gate" }] })).toBe(false);
    expect(rulesHaveNothing({ caps, speakers: [] })).toBe(false);
  });
});

describe("the camera's own speaker for talk-down", () => {
  const cam = { id: "cam1", host: "10.0.0.5" };
  it("offered with an audio output and no linked speaker at the camera's address", () => {
    expect(offerCameraSpeaker(null, [], cam)).toBe(false);
    expect(offerCameraSpeaker({ audio_outputs: 0 }, [], cam)).toBe(false);
    expect(offerCameraSpeaker({ audio_outputs: 1 }, [], cam)).toBe(true);
    expect(offerCameraSpeaker({ audio_outputs: 1 }, [{ host: "10.0.0.9", camera_ids: ["cam1"] }], cam)).toBe(true);
    expect(offerCameraSpeaker({ audio_outputs: 1 }, [{ host: "10.0.0.5", camera_ids: ["cam2"] }], cam)).toBe(true);
    expect(offerCameraSpeaker({ audio_outputs: 1 }, [{ host: "10.0.0.5", camera_ids: ["cam1"] }], cam)).toBe(false);
  });
});

describe("Milesight extras", () => {
  const m: MilesightExtras = {
    supported: true,
    white_light: { supported: true, mode: "auto", sensitivity: 3, delay_s: 10, start: "19:00", stop: "06:00", brightness: 80, writable: true },
    red_blue: { supported: true, mode: "red_blue", frequency: "medium", writable: true },
    siren: { supported: true, files: [{ index: 0, name: "Alarm", present: true }], play_index: 0, speaker_volume: 60, volume_writable: true, file_writable: false },
    disarm: { supported: true, enable: false, mutes: { record: false, output: true }, writable: true },
  };
  it("an unchanged form sends nothing", () => {
    expect(milesightChanges(m, milesightDraft(m))).toEqual({});
    expect(milesightProblems(m, milesightDraft(m))).toEqual({});
  });
  it("only the fields that changed, as numbers", () => {
    const d = milesightDraft(m);
    d.white_light = { ...d.white_light, mode: "schedule", start: "20:30", brightness: "90" };
    d.red_blue.frequency = "high";
    d.speaker_volume = "75";
    d.disarm = { enable: true, mutes: { record: true, output: true } };
    const body = milesightChanges(m, d);
    expect(body).toEqual({ white_light: { mode: "schedule", start: "20:30", brightness: 90 }, red_blue: { frequency: "high" }, speaker_volume: 75,
      disarm: { enable: true, mutes: { record: true } } });
    expect(sectionBody(body, "speaker_volume")).toEqual({ speaker_volume: 75 });
    expect(sectionBody(body, "red_blue")).toEqual({ red_blue: { frequency: "high" } });
    expect(sectionBody({}, "disarm")).toEqual({});
  });
  it("never writes what the camera won't take", () => {
    const ro: MilesightExtras = { ...m, white_light: { ...m.white_light!, writable: false }, siren: { ...m.siren!, volume_writable: false },
      disarm: { ...m.disarm!, writable: false }, red_blue: { ...m.red_blue!, supported: false, reason: "no red/blue light" } };
    const d = milesightDraft(ro);
    d.white_light.mode = "on"; d.speaker_volume = "10"; d.disarm.enable = true; d.red_blue.mode = "red";
    expect(milesightChanges(ro, d)).toEqual({});
  });
  it("ranges per section, only where they apply", () => {
    const d = milesightDraft(m);
    d.white_light = { ...d.white_light, sensitivity: "9", delay_s: "", brightness: "0" };
    d.speaker_volume = "101";
    expect(milesightProblems(m, d)).toEqual({ white_light: ["Sensitivity must be 1–5", "Delay must be 1–60 s", "Brightness must be 1–100"],
      speaker_volume: ["Speaker volume must be 0–100"] });
    d.white_light = { ...d.white_light, mode: "schedule", start: "7pm" };
    expect(milesightProblems(m, d).white_light).toEqual(["Start and stop are times like 19:30", "Brightness must be 1–100"]);
    d.white_light.mode = "off";
    expect(milesightProblems(m, d).white_light).toBeUndefined();
  });
  it("a camera that isn't a Milesight drafts defaults without failing", () => {
    expect(milesightDraft({ supported: false, reason: "not a Milesight" }).white_light.mode).toBe("auto");
    expect(milesightChanges({ supported: false }, milesightDraft({ supported: false }))).toEqual({});
  });
});

describe("the siren through a speaker", () => {
  const spk = (own: boolean) => ({ via: "speaker" as const, target: "spk3", label: "Siren through the camera's speaker", timed: true, pulse_s: null,
    speaker_id: "spk3", speaker: "Q gate speaker", own_speaker: own });
  const route = (p: Partial<SirenRoute>): SirenRoute => ({ via: "speaker", source: "auto", speaker_id: "spk3", speaker: "Q gate speaker", own_speaker: true,
    camera_sounds: false, has_output: true, speakers: 1, reason: "the camera's siren has no sound file loaded", ...p });
  it("the button says which way it goes", () => {
    const d = { ...base, actions: { ...base.actions, siren: spk(true) },
      relays: [{ token: "Sounder", mode: "monostable" as const, delay_s: 10, idle_state: "closed", role: "siren" as const }] };
    const b = deterButtons(d);
    expect(b.map((x) => x.key)).toEqual(["siren"]);   // the silent Sounder relay is the siren's, not a button of its own
    expect(b[0]).toMatchObject({ action: "siren", label: "Siren", timed: true, title: "Siren — through the camera's speaker" });
    expect(sirenSpeakerTitle(spk(false))).toBe("Siren — through speaker Q gate speaker");
    expect(sirenSpeakerTitle({ ...spk(false), speaker: null })).toBe("Siren — through speaker spk3");
    expect(deterRequest(b[0], false, 30)).toEqual({ action: "siren", on: true, seconds: 30 });
  });
  it("the last action says it went through the speaker", () => {
    const last = { ts: 1, action: "siren", on: true, seconds: 10, by: "op@x", source: "manual" as const, result: "ok" as const, error: null };
    expect(lastText({ ...last, via: "speaker" })).toBe("Siren on for 10 s through the speaker · op@x");
    expect(lastText({ ...last, via: "relay" })).toBe("Siren on for 10 s · op@x");
  });
  it("the setting offers only the ways the camera has, and says which way it goes now", () => {
    expect(sirenSourceChoices(route({}))).toEqual(["auto", "relay", "speaker"]);
    expect(sirenSourceChoices(route({ has_output: false }))).toEqual(["auto", "speaker"]);
    expect(sirenSourceChoices(route({ speakers: 0 }))).toEqual(["auto", "relay"]);
    expect(sirenSourceChoices(route({ speakers: 0 }), "speaker")).toEqual(["auto", "relay", "speaker"]);   // the saved one stays listed
    expect(sirenSourceChoices(null)).toEqual(["auto"]);
    expect(sirenSourceName("relay")).toBe("The camera's own siren");
    expect(sirenWayText(route({}))).toBe("The siren sounds through the camera's speaker (the camera's siren has no sound file loaded).");
    expect(sirenWayText(route({ own_speaker: false, reason: "" }))).toBe("The siren sounds through speaker Q gate speaker.");
    expect(sirenWayText(route({ via: "relay", reason: "the camera has a siren sound loaded" })))
      .toBe("The siren sounds on the camera's own siren output (the camera has a siren sound loaded).");
    expect(sirenWayText(route({ via: null, reason: "no siren output" }))).toBe("No siren (no siren output).");
    expect(sirenWayText(null)).toMatch(/hasn't been asked/);
  });
});
