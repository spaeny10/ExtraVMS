"""What a camera says about an object beyond its box (camattrs.py), from a Milesight MS-C8477-HPG1 pano on firmware
63.8.0.6-r2 (TSS-DT cam10, 2026-11-12): vehicle type and color, clothing colors, helmet and hat. Its face boxes come
numbered apart from the bodies, often in documents of their own, and opened a second person event (32204) beside the
body's: faces on a camera that boxes bodies are parts of people now. Some likelihoods come as percent ("53.00").

The documents below are trimmed copies of what the camera sent. No camera, MediaMTX, GPU or network.

Run: ..\\.venv\\Scripts\\python.exe tests\\test_camera_attrs.py   (from backend/)
"""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="nvr-camattrs-test-"))
for k, sub in (("NVR_DATA_DIR", "data"), ("NVR_RECORDINGS_DIR", "recordings"), ("NVR_RUNTIME_DIR", "runtime")):
    os.environ[k] = str(TMP / sub)   # never the real server's database, recordings or mediamtx.yml
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nvr import camattrs, merge  # noqa: E402
from nvr.config import settings  # noqa: E402
from nvr.db import db  # noqa: E402
from nvr.ingest import DetectedObject, MetaFrame, parse_metadata  # noqa: E402
from nvr.tracker import FACE_WITH_BODY_S, Tracker  # noqa: E402

for d in (settings.data_dir, settings.recordings_dir, settings.runtime_dir):
    v = str(d).replace("\\", "/").lower()
    assert "nvr-camattrs-test-" in v and "e:/nvr" not in v and "d:/nvr" not in v, d

db.upsert_camera({"id": "cam10", "name": "8477 Pano East", "host": "10.0.0.61", "zones": []})

HEAD = ('<?xml version="1.0" encoding="UTF-8"?><tt:MetadataStream xmlns:tt="http://www.onvif.org/ver10/schema" '
        'xmlns:fc="http://www.onvif.org/ver20/analytics/humanface" xmlns:bd="http://www.onvif.org/ver20/analytics/humanbody">'
        '<tt:VideoAnalytics><tt:Frame UtcTime="{ts}"><tt:SimpleItem Name="DeviationTime" Value="0"></tt:SimpleItem>')
TAIL = '</tt:Frame></tt:VideoAnalytics></tt:MetadataStream>'
TRANSFORM = ('<tt:Transformation><tt:Translate x="-1.000000" y="1.000000"></tt:Translate>'
             '<tt:Scale x="0.006250" y="-0.008333"></tt:Scale></tt:Transformation>')


def shape(l, t, r, b, kind):
    return (f'<tt:Shape><tt:BoundingBox left="{l}" top="{t}" right="{r}" bottom="{b}"></tt:BoundingBox>'
            f'<tt:Extension><tt:ShapeDescriptorExtension><tt:ObjectType>{kind}</tt:ObjectType>'
            '</tt:ShapeDescriptorExtension></tt:Extension></tt:Shape>')


def obj(oid, *parts):
    return f'<tt:Object ObjectId="{oid}"><tt:Appearance>{TRANSFORM}{"".join(parts)}</tt:Appearance></tt:Object>'


def doc(*objects, ts="2026-11-12T16:09:07.203Z"):
    return (HEAD.format(ts=ts) + "".join(objects) + TAIL).encode()


def cls(kind, lk):
    return f'<tt:Class><tt:Type Likelihood="{lk}">{kind}</tt:Type></tt:Class>'


def helmet(worn):
    return f'<tt:HumanFace><fc:Accessory><fc:Helmet><fc:Wear>{worn}</fc:Wear></fc:Helmet></fc:Accessory></tt:HumanFace>'


def color(name, lk, rgb=(0, 0, 0)):
    x, y, z = rgb
    return (f'<tt:Color><tt:ColorCluster><tt:Color X="{x}" Y="{y}" Z="{z}" Likelihood="{lk}" Colorspace="RGB"'
            + (f' ColorName="{name}"' if name else "") + '></tt:Color></tt:ColorCluster></tt:Color>')


# a person with a helmet read, its likelihood in percent (as sent)
HUMAN_HELMET = obj("19", shape(209.0, 127.0, 214.0, 139.0, "Human"), cls("Human", "53.00"), helmet("True"))
# clothing and a hat for track 276, with no box: an Object of its own
CLOTHING_276 = obj("276", '<tt:HumanBody><bd:Clothing><bd:Tops>' + color("Green", "0.88", (0, 128, 0)) + '</bd:Tops>'
                   '<bd:Bottoms>' + color("Black", "0.00") + '</bd:Bottoms></bd:Clothing>'
                   '<tt:ImageRef>../dataloader.cgi?dw=picture&amp;path=/tmp/objattr/1794499745403_1_276.jpg</tt:ImageRef>'
                   '</tt:HumanBody><tt:HumanFace><fc:Accessory><fc:Hat><fc:Wear>False</fc:Wear></fc:Hat></fc:Accessory></tt:HumanFace>')
# a vehicle whose VehicleInfo/Type sits beside its Class/Type
CAR_282 = obj("282", shape(23.0, 87.0, 26.0, 91.0, "Vehicle"), cls("Vehicle", "0.36"), '<tt:VehicleInfo><tt:Type>Car</tt:Type></tt:VehicleInfo>')
TRUCK_295 = obj("295", '<tt:VehicleInfo><tt:Type>Truck</tt:Type>' + color("White", "0.76", (255, 255, 255))
                + '<tt:ImageRef>../dataloader.cgi?dw=picture&amp;path=/tmp/objattr/1794499769277_2_295.jpg</tt:ImageRef>'
                '</tt:VehicleInfo>', cls("Vehicle", "0.76"))
FACE_9 = obj("9", shape(250.0, 104.0, 252.0, 109.0, "Face"), cls("Face", "0.00"))


# ---------------------------------------------------------------- parsing

def test_parses_helmet_and_percent_likelihood():
    [f] = parse_metadata("cam10", doc(HUMAN_HELMET))
    [o] = f.objects
    assert o.cls == "Human" and abs(o.conf - 0.53) < 1e-6, o   # "53.00" is percent, not 53
    assert o.attrs == {"helmet": ("yes", camattrs.UNRATED)} and f.attrs == {}


def test_attributes_without_a_box_are_kept_by_object_id():
    [f] = parse_metadata("cam10", doc(CLOTHING_276, TRUCK_295))
    assert f.objects == []   # neither has a box: nothing to track, but what they say belongs to tracks 276 and 295
    assert f.attrs["276"] == {"top_color": ("green", 0.88), "bottom_color": ("black", camattrs.UNRATED), "hat": ("no", camattrs.UNRATED)}
    assert f.attrs["295"] == {"vehicle_type": ("truck", camattrs.UNRATED), "vehicle_color": ("white", 0.76)}


def test_vehicle_type_is_an_attribute_not_the_class():
    [f] = parse_metadata("cam10", doc(CAR_282, FACE_9))
    car, face = f.objects
    assert car.cls == "Vehicle" and abs(car.conf - 0.36) < 1e-6 and car.attrs == {"vehicle_type": ("car", camattrs.UNRATED)}
    assert face.cls == "Face" and face.attrs == {}


def test_rgb_without_a_name_gets_the_nearest_one():
    [f] = parse_metadata("cam10", doc(obj("5", '<tt:VehicleInfo>' + color(None, "0.9", (250, 10, 5)) + '</tt:VehicleInfo>')))
    assert f.attrs["5"] == {"vehicle_color": ("red", 0.9)}


# ---------------------------------------------------------------- votes

def test_votes_best_firm_and_merge():
    v: dict = {}
    for _ in range(4):
        camattrs.add(v, {"helmet": ("no", 0.5), "top_color": ("green", 0.9)}, "person")
    camattrs.add(v, {"helmet": ("yes", 0.5), "vehicle_type": ("car", 0.5)}, "person")   # a person has no vehicle type
    assert "vehicle_type" not in v and v["helmet"] == {"no": [4, 2.0], "yes": [1, 0.5]}
    assert camattrs.best(v, "helmet") == {"value": "no", "n": 4, "share": 0.8}
    assert camattrs.firm(v, "helmet") == "no" and camattrs.firm(v, "top_color") == "green"
    assert camattrs.firm({"helmet": {"yes": [2, 1.0]}}, "helmet") is None   # twice is not enough
    both = camattrs.merge(v, {"helmet": {"yes": [5, 2.5]}})
    assert both["helmet"] == {"no": [4, 2.0], "yes": [6, 3.0]} and camattrs.best(both, "helmet")["value"] == "yes"
    assert camattrs.merge(None, None) is None


def test_describe_and_ppe_calls():
    person = {"top_color": {"green": [7, 6.0]}, "bottom_color": {"black": [7, 3.5]}, "helmet": {"yes": [5, 2.5]}}
    assert camattrs.describe(person, "person") == "green top, black bottoms, wearing a hard hat"
    assert camattrs.describe({"vehicle_type": {"truck": [3, 1.5]}, "vehicle_color": {"white": [1, 0.76]}}, "vehicle") == "white truck"
    assert camattrs.describe({"vehicle_color": {"white": [1, 0.76]}}, "vehicle") == "white vehicle"
    assert camattrs.describe(None, "person") is None
    assert camattrs.ppe_calls(person, ["hard_hat", "vest"]) == {"hard_hat": "present", "vest": "unclear"}
    assert camattrs.ppe_calls({"helmet": {"no": [6, 3.0]}}, ["hard_hat"]) == {"hard_hat": "missing"}
    assert camattrs.ppe_calls({"helmet": {"no": [2, 1.0]}}, ["hard_hat"]) == {"hard_hat": "unclear"}


# ---------------------------------------------------------------- the tracker

class Closed(list):
    async def __call__(self, eid):
        self.append(eid)


def tracker():
    tr = Tracker(Closed())
    tr.camera = lambda cid: None
    return tr


BODY = (0.40, 0.40, 0.50, 0.80)
FACE = (0.43, 0.40, 0.46, 0.45)


def test_a_face_is_part_of_the_body_the_camera_boxes():
    tr = tracker()
    t0 = time.time()
    for i in range(int(settings.track_min_seconds) + 3):
        # body and face in turns, the face numbered apart (as the pano sends them)
        tr.on_frame(MetaFrame("cam10", t0 + i, [DetectedObject("276", "Human", 0.9, BODY, {"top_color": ("green", 0.9)})]))
        tr.on_frame(MetaFrame("cam10", t0 + i + 0.5, [DetectedObject("9", "Face", 0.0, FACE)],
                              {"276": {"bottom_color": ("black", 0.5)}}))
    assert set(k[1] for k in tr.tracks) == {"276"}, tr.tracks   # no second person from the face
    t = tr.tracks[("cam10", "276")]
    assert t.event_id is not None and t.attrs == {"top_color": {"green": [len(t.path), round(0.9 * len(t.path), 3)]},
                                                   "bottom_color": {"black": [len(t.path), round(0.5 * len(t.path), 3)]}}
    for tt in tr.tracks.values():
        tt.last_wall = 0.0
    asyncio.run(tr.sweep())
    e = db.event(t.event_id)
    assert e["camera_attrs"]["top_color"]["green"][0] == len(t.path) and e["camera_attrs"]["bottom_color"]
    assert e["camera_conf"] <= 1.0


def test_faces_still_count_on_a_camera_that_never_boxes_bodies():
    tr = tracker()
    t0 = time.time()
    for i in range(int(settings.track_min_seconds) + 2):
        tr.on_frame(MetaFrame("cam10", t0 + i, [DetectedObject("9", "Face", 0.8, FACE)]))
    assert tr.tracks[("cam10", "9")].event_id is not None
    # and again once the last body is long ago
    tr2 = tracker()
    tr2.body_seen["cam10"] = time.time() - FACE_WITH_BODY_S - 1
    tr2.on_frame(MetaFrame("cam10", t0, [DetectedObject("9", "Face", 0.8, FACE)]))
    assert ("cam10", "9") in tr2.tracks


def test_helmet_looks_sent_as_new_objects_go_to_the_person_under_them():
    """The pano's helmet check sends each look as a one-frame "Human" with ids 0, 1, 2... on top of body 276."""
    tr = tracker()
    t0 = time.time()
    other = (0.70, 0.40, 0.80, 0.80)
    for i in range(6):
        tr.on_frame(MetaFrame("cam10", t0 + i, [DetectedObject("276", "Human", 0.9, BODY),
                                               DetectedObject("280", "Human", 0.9, other)]))
        tr.on_frame(MetaFrame("cam10", t0 + i + 0.5, [DetectedObject(str(i), "Human", 0.5, FACE, {"helmet": ("no", 0.5)})]))
    # the first look came when 276 had one sample (not established yet): it stayed with its own one-frame track
    assert tr.tracks[("cam10", "276")].attrs == {"helmet": {"no": [5, 2.5]}}
    assert tr.tracks[("cam10", "280")].attrs == {} and tr.tracks[("cam10", "1")].attrs == {}
    # nothing established under it: the object keeps what it said
    tr.on_frame(MetaFrame("cam10", t0 + 9, [DetectedObject("50", "Human", 0.5, (0.05, 0.05, 0.1, 0.1), {"helmet": ("yes", 0.5)})]))
    assert tr.tracks[("cam10", "50")].attrs == {"helmet": {"yes": [1, 0.5]}}


def test_vehicle_attributes_reach_the_event_and_skip_person_keys():
    tr = tracker()
    t0 = time.time()
    box = (0.1, 0.4, 0.3, 0.6)
    for i in range(int(settings.track_min_seconds) + 2):
        tr.on_frame(MetaFrame("cam10", t0 + i, [DetectedObject("295", "Vehicle", 0.76, box, {"vehicle_type": ("truck", 0.5)})],
                              {"295": {"vehicle_color": ("white", 0.76), "top_color": ("green", 0.9)}}))
    t = tr.tracks[("cam10", "295")]
    assert set(t.attrs) == {"vehicle_type", "vehicle_color"} and t.event_id is not None
    e = db.event(t.event_id)
    assert camattrs.describe(e["camera_attrs"], "vehicle") == "white truck"   # stored when the event opened


# ---------------------------------------------------------------- merge and search

def test_merged_events_keep_both_reads():
    now = time.time() - 3600
    a = db.create_event(camera_id="cam10", track_id="276", camera_class="person", start_ts=now, end_ts=now + 10,
                        path=[[now, *BODY, 0.9]], status="verified", camera_attrs={"top_color": {"green": [3, 2.7]}})
    b = db.create_event(camera_id="cam10", track_id="297", camera_class="person", start_ts=now + 12, end_ts=now + 20,
                        path=[[now + 12, *BODY, 0.9]], status="verified", camera_attrs={"top_color": {"green": [2, 1.8]}, "helmet": {"no": [4, 2.0]}})
    merge.apply(db.event(a), db.event(b), "test")
    e = db.event(a)
    assert e["camera_attrs"] == {"top_color": {"green": [5, 4.5]}, "helmet": {"no": [4, 2.0]}}, e["camera_attrs"]


def test_search_finds_what_the_camera_saw():
    from nvr import pipeline as pl

    async def embed(text):
        return None
    pl.vlm.embed = embed
    p = pl.Pipeline()
    now = time.time() - 7200
    eid = db.create_event(camera_id="cam10", track_id="295", camera_class="vehicle", start_ts=now, end_ts=now + 8,
                          path=[[now, 0.1, 0.4, 0.3, 0.6, 0.76]], status="verified",
                          camera_attrs={"vehicle_type": {"truck": [4, 2.0]}, "vehicle_color": {"white": [2, 1.5]}})
    asyncio.run(p.reindex(eid))
    assert any(h["id"] == eid for h in db.search("white truck", None))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
    print("all passed")
