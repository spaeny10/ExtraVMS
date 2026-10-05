import { describe, expect, it } from "vitest";
import {
  type GeoHit, type PlaceForm, applyDrag, applyHit, cityLine, cityState, dispatchLines, dispatchText, fmtCoord, formatAddress, getMapConfig,
  hasPoint, OSM_TILES, pinColour, pinStatus, placePatch, setMapConfig, streetLine, tzReplaceable,
} from "./place";

const AUGUSTA = { house_number: "1200", street: "Main Street", city: "Augusta", county: "Butler County", state: "Kansas", state_code: "KS",
  postcode: "67010", country: "United States", country_code: "US", display_name: "1200, Main Street, Augusta, Butler County, Kansas, 67010, United States" };
const HIT: GeoHit = { display: "1200 Main Street, Augusta, KS 67010, United States", address_parts: AUGUSTA, lat: 37.68668, lon: -96.9767, timezone: "America/Chicago" };
const blank: PlaceForm = { address: "1200 main", timezone: "", lat: null, lon: null, address_parts: null, source: null, autoTz: null };

describe("address formatting from parts", () => {
  it("formats a US address the way the hub does", () => {
    expect(streetLine(AUGUSTA)).toBe("1200 Main Street");
    expect(cityLine(AUGUSTA)).toBe("Augusta, KS 67010");
    expect(formatAddress(AUGUSTA)).toBe(HIT.display);
    expect(cityState(AUGUSTA)).toBe("Augusta, KS");
  });
  it("puts the number after the street where that is the custom, and spells out regions", () => {
    const de = { street: "Unter den Linden", house_number: "77", city: "Berlin", state: "Berlin", postcode: "10117", country: "Germany", country_code: "DE" };
    expect(streetLine(de)).toBe("Unter den Linden 77");
    expect(formatAddress(de)).toBe("Unter den Linden 77, Berlin, Berlin 10117, Germany");
  });
  it("copes with missing parts", () => {
    expect(formatAddress({ city: "Augusta", display_name: "x" })).toBe("Augusta");
    expect(formatAddress({ display_name: "Somewhere" })).toBe("Somewhere");
    expect(formatAddress(null)).toBe("");
    expect(cityLine({ postcode: "67010" })).toBe("67010");
  });
  it("dispatch lines: street, city line, country when not the US; the typed address without parts", () => {
    expect(dispatchLines({ address: HIT.display, address_parts: AUGUSTA })).toEqual(["1200 Main Street", "Augusta, KS 67010"]);
    expect(dispatchLines({ address: "Dock 4, rear gate", address_parts: null })).toEqual(["Dock 4, rear gate"]);
    expect(dispatchLines({ address: "", address_parts: null })).toEqual([]);
    expect(dispatchLines({ address: "x", address_parts: { city: "Leeds", country: "United Kingdom", country_code: "GB" } })).toEqual(["x", "Leeds", "United Kingdom"]);
    expect(dispatchText({ name: "Yard", address: HIT.display, address_parts: AUGUSTA, lat: 37.68668, lon: -96.9767 }))
      .toBe("Yard\n1200 Main Street\nAugusta, KS 67010\nGPS 37.68668, -96.97670");
  });
  it("coordinates to 5 decimals; hasPoint", () => {
    expect(fmtCoord(37.686681234, -96.9767)).toBe("37.68668, -96.97670");
    expect(hasPoint({ lat: 0, lon: 0 })).toBe(true);
    expect(hasPoint({ lat: null, lon: 1 })).toBe(false);
    expect(hasPoint(null)).toBe(false);
  });
});

describe("suggestion and marker → form fields", () => {
  it("a pick fills address, parts, point and an empty time zone", () => {
    const { form, tzSet } = applyHit(blank, HIT);
    expect(form).toMatchObject({ address: HIT.display, lat: 37.68668, lon: -96.9767, timezone: "America/Chicago", autoTz: "America/Chicago", source: "nominatim" });
    expect(form.address_parts?.city).toBe("Augusta");
    expect(tzSet).toBe(true);
  });
  it("records which geocoder the pick came from", () => {
    expect(applyHit(blank, { ...HIT, source: "census" }).form.source).toBe("census");
  });
  it("never replaces a time zone someone typed", () => {
    const { form, tzSet } = applyHit({ ...blank, timezone: "America/Denver" }, HIT);
    expect(form.timezone).toBe("America/Denver");
    expect(tzSet).toBe(false);
  });
  it("replaces the time zone the previous pick set", () => {
    const first = applyHit(blank, { ...HIT, timezone: "America/New_York" }).form;
    expect(tzReplaceable(first)).toBe(true);
    expect(applyHit(first, HIT).form.timezone).toBe("America/Chicago");
  });
  it("a drag moves the point, keeps the address, and updates the time zone the same way", () => {
    const picked = applyHit(blank, HIT).form;
    const { form, tzSet } = applyDrag(picked, 39.7392351, -104.9902511, "America/Denver");
    expect(form).toMatchObject({ address: HIT.display, lat: 39.7392351, lon: -104.9902511, timezone: "America/Denver", source: "marker" });
    expect(tzSet).toBe(true);
    expect(applyDrag({ ...picked, timezone: "Europe/Paris", autoTz: "America/Chicago" }, 1, 2, "America/Denver").form.timezone).toBe("Europe/Paris");
  });
  it("the PATCH carries only what changed", () => {
    const saved = { lat: 37.68668, lon: -96.9767, address_parts: AUGUSTA };
    expect(placePatch({ ...blank, lat: 37.68668, lon: -96.9767, address_parts: AUGUSTA }, saved)).toEqual({});
    expect(placePatch({ ...blank, lat: 37.7, lon: -96.9767, address_parts: AUGUSTA, source: "marker" }, saved)).toEqual({ lat: 37.7, lon: -96.9767, geocode_source: "marker" });
    expect(placePatch(applyHit(blank, HIT).form, {})).toEqual({ lat: 37.68668, lon: -96.9767, geocode_source: "nominatim", address_parts: AUGUSTA });
  });
});

describe("pins and tiles", () => {
  it("colours a pin by status: alerts, then offline, then online", () => {
    expect(pinStatus({ open_alerts: 2, servers_total: 2, servers_online: 0 })).toBe("alerts");
    expect(pinStatus({ open_alerts: 0, servers_total: 2, servers_online: 1 })).toBe("offline");
    expect(pinStatus({ open_alerts: 0, servers_total: 2, servers_online: 2 })).toBe("online");
    expect(pinStatus({ open_alerts: 0, servers_total: 0, servers_online: 0 })).toBe("empty");
    expect(pinColour({ open_alerts: 1, servers_total: 1, servers_online: 1 })).toContain("--bad");
    expect(pinColour({ open_alerts: 0, servers_total: 1, servers_online: 1 })).toContain("--ok");
  });
  it("uses OSM tiles until the hub says otherwise", () => {
    expect(getMapConfig()).toEqual(OSM_TILES);
    setMapConfig(undefined);
    expect(getMapConfig()).toEqual(OSM_TILES);
    setMapConfig({ tiles: "https://tiles.example.com/{z}/{x}/{y}.png", attribution: "Example" });
    expect(getMapConfig().tiles).toBe("https://tiles.example.com/{z}/{x}/{y}.png");
  });
});
