import { describe, expect, it } from "vitest";
import { plateAlarms, plateLabel, serversNote, vehicleLabel, vehiclesSummary } from "./vehicles";

const t = (ts: number) => `t${ts}`;

describe("vehicles around an incident", () => {
  it("labels the plate by who read it", () => {
    expect(plateLabel({ plate: "ABC1234", plate_by: "camera" })).toBe("ABC 1234");
    expect(plateLabel({ plate: "27654", plate_by: "otso" })).toBe("27654 (Otso)");
    expect(plateLabel({ plate: "FTG4321", plate_by: "footage" })).toBe("FTG 4321 (from footage)");
    expect(plateLabel({ plate: null, plate_by: null })).toBe("No plate");
  });
  it("says what it is and which way it went, the camera's words as a guess", () => {
    expect(vehicleLabel({ vehicle: "silver Chevrolet Silverado pickup truck", vehicle_by: "otso", direction: "approach" }))
      .toBe("silver Chevrolet Silverado pickup truck · toward the camera (reported)");
    expect(vehicleLabel({ vehicle: "white GMC truck", vehicle_by: "camera", direction: "away" }))
      .toBe("white GMC truck (the camera's guess) · going away (reported)");
    expect(vehicleLabel({ vehicle: null, vehicle_by: "camera", direction: null })).toBe("");
  });
  it("sums up the window", () => {
    const v = (plate: string | null) => ({ plate });
    expect(vehiclesSummary({ since: 1, until: 2, vehicles: [v("A1"), v(null), v("B2")] as never }, t)).toBe("3 vehicles, 2 with a plate, from t1 to t2.");
    expect(vehiclesSummary({ since: 1, until: 2, vehicles: [v(null)] as never }, t)).toBe("1 vehicle, none with a plate, from t1 to t2.");
    expect(vehiclesSummary({ since: 1, until: 2, vehicles: [] }, t)).toBe("No vehicles seen from t1 to t2.");
    expect(vehiclesSummary({ since: 1, until: 2, total: 362, vehicles: [v("A1"), v("B2")] as never }, t))
      .toBe("2 of 362 vehicles from t1 to t2: those with a plate nearest the incident.");
    expect(vehiclesSummary({ since: 1, until: 2, total: 1, vehicles: [v("A1")] as never }, t)).toBe("1 vehicle, 1 with a plate, from t1 to t2.");
  });
  it("names the servers that didn't answer", () => {
    expect(serversNote({ servers: [
      { server_id: "a", server_name: "Yard NVR", status: "offline" },
      { server_id: "b", server_name: "Gate NVR", status: "timeout", error: "no answer within 10 s" },
      { server_id: "c", server_name: "Ok NVR", status: "ok" },
    ] })).toBe("Yard NVR is offline; Gate NVR: no answer within 10 s");
    expect(serversNote({ servers: [] })).toBe("");
  });
  it("lifts the incident's plate alarms", () => {
    expect(plateAlarms([
      { detail: { policy: { kind: "plate_pattern", text: "Plate CRC 1234 passed 3 times in 20 minutes" } } },
      { detail: { policy: { kind: "plate_pattern", text: "Plate CRC 1234 passed 3 times in 20 minutes" } } },
      { detail: { policy: { kind: "entry", text: "Entered through Gate" } } },
      { detail: { policy: { kind: "plate_list", text: "Plate ABC 1234 is on the deny list" } } },
      { detail: null },
    ] as never)).toEqual(["Plate CRC 1234 passed 3 times in 20 minutes", "Plate ABC 1234 is on the deny list"]);
  });
});
