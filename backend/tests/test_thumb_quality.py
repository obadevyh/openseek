"""Thumbnail quality presets: defaults and invalid values, persistence, real output geometry and JPEG quality,
quality-aware cache (separation, reuse, legacy caches), VTT coordinates, and changes during generation."""
from __future__ import annotations

import io
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from PIL import Image

from helpers import FPS, decode_barcode, make_policy, parse_vtt, wait_for
from openseek import quality as Q
from openseek.cache import PROFILE, Cache, jpeg_size
from openseek.config import Settings
from openseek.db import DB
from openseek.fetch import RangeProxy
from openseek.jobs import JobManager
from openseek.vtt import render
from openseek.worker import GrabResult, OpenResult

HOST = "media.example.com"
FP1 = "ab" * 32


def jpeg(w: int, h: int, color=(200, 10, 10), q: int = 75) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), color).save(b, "JPEG", quality=q)
    return b.getvalue()


def qtables(q: int):
    with Image.open(io.BytesIO(jpeg(64, 64, q=q))) as im:
        return im.quantization


# ----------------------------------------------------------------------------- presets and configuration values
def test_presets_and_default():
    assert Q.DEFAULT is Q.STANDARD
    assert [(q.name, q.w, q.h, q.jpeg_q) for q in Q.PRESETS] == [
        ("low", 160, 90, 60), ("standard", 320, 180, 75), ("high", 480, 270, 85), ("max", 640, 360, 90)]
    assert Q.STANDARD.profile == PROFILE == "t320x180q75g10v1"        # the original cache layout, unchanged
    for q in Q.PRESETS:
        sw, sh = q.sheet_size
        assert sw <= 3200 and sh <= 1800                               # the Nuvio client's image limit
    assert len({q.profile for q in Q.PRESETS}) == 4


@pytest.mark.parametrize("raw", [None, "", "ultra", "320", "  ", 5, "Standard ", ["high"]])
def test_missing_or_invalid_values_fall_back_to_standard(raw):
    assert Q.parse(raw) is Q.STANDARD


def test_parse_is_case_insensitive_and_wire_is_strict():
    assert Q.parse("HIGH") is Q.HIGH and Q.parse(" max ") is Q.MAX
    assert Q.from_wire([640, 360, 90]) is Q.MAX
    for bad in ([640, 360, 91], [9999, 9999, 90], "640x360", [640, 360], [True, 1, 1], None):
        assert Q.from_wire(bad) is None                               # the worker renders only exact presets


def test_core_default_and_invalid_stored_value(tmp_path):
    from openseek.app import Core

    c = Core(Settings().with_(data_dir=tmp_path / "a", admin_token="test-admin-token"))
    assert c.jobs.quality is Q.STANDARD and c.cache.quality is Q.STANDARD   # new install: Standard
    c.db.set_setting(Q.SETTING_KEY, "bogus")
    c.db.close()
    c2 = Core(Settings().with_(data_dir=tmp_path / "a", admin_token="test-admin-token"))
    assert c2.jobs.quality is Q.STANDARD                               # unreadable value: Standard, no crash
    c2.db.close()


def test_preference_persists_across_restart(tmp_path):
    from openseek.app import Core

    c = Core(Settings().with_(data_dir=tmp_path / "a", admin_token="test-admin-token"))
    c.set_thumb_quality(Q.HIGH)
    c.db.close()
    c2 = Core(Settings().with_(data_dir=tmp_path / "a", admin_token="test-admin-token"))
    assert c2.jobs.quality is Q.HIGH and c2.cache.quality is Q.HIGH
    c2.db.close()


# ----------------------------------------------------------------------------- cache
@pytest.fixture()
def cache(tmp_path):
    db = DB(tmp_path / "t.db")
    yield Cache(tmp_path / "data", db, 1 << 30, 30)
    db.close()


def test_cache_separates_qualities(cache, tmp_path):
    std, hi = jpeg(320, 180, (10, 10, 10)), jpeg(480, 270, (250, 250, 250))
    cache.put_tile(FP1, 3, 30_000, std)
    cache.put_tile(FP1, 3, 30_100, hi, Q.HIGH)
    assert cache.tile_bytes(FP1, 3) == std and cache.tile_bytes(FP1, 3, Q.HIGH) == hi
    assert cache.tile_bytes(FP1, 3, Q.MAX) is None                     # never another quality's tile
    assert cache.entry(FP1).tiles == {3: 30_000} and cache.entry(FP1, Q.HIGH).tiles == {3: 30_100}
    assert (tmp_path / "data" / "cache" / FP1[:2] / FP1 / Q.HIGH.profile / "tiles" / "3.jpg").is_file()
    assert {q.name for q in cache.cached_qualities(FP1)} == {"standard", "high"}
    with pytest.raises(ValueError):
        cache.put_tile(FP1, 4, 40_000, std, Q.HIGH)                    # wrong size for the preset
    with pytest.raises(ValueError):
        cache.put_tile(FP1, 4, 40_000, hi)                             # (and the other way round)
    cache.drop_tile(FP1, 3, Q.HIGH)
    assert cache.tile_bytes(FP1, 3) == std                             # dropping one quality keeps the others


def test_version_is_shared_and_monotonic(cache):
    v1 = cache.put_tile(FP1, 0, 0, jpeg(320, 180))
    v2 = cache.put_tile(FP1, 0, 0, jpeg(640, 360), Q.MAX)
    assert v2 > v1 and cache.entry(FP1).version == cache.entry(FP1, Q.MAX).version == v2
    cache.bump_versions()
    assert cache.entry(FP1).version == v2 + 1


def test_cache_reuse_after_restart_and_byte_accounting(tmp_path):
    db = DB(tmp_path / "t.db")
    c = Cache(tmp_path / "data", db, 1 << 30, 30)
    a, b = jpeg(320, 180), jpeg(640, 360)
    c.put_tile(FP1, 1, 10_000, a)
    c.put_tile(FP1, 1, 10_000, b, Q.MAX)
    db.close()
    db = DB(tmp_path / "t.db")
    c2 = Cache(tmp_path / "data", db, 1 << 30, 30, quality=Q.MAX)
    assert c2.tile_bytes(FP1, 1) == b and c2.tile_bytes(FP1, 1, Q.STANDARD) == a
    assert c2.total_bytes() == len(a) + len(b)
    assert c2.stats()["tiles"] == 1 and c2.stats()["quality"] == "max"
    assert c2.remove_entry(FP1)                                        # one title = every quality
    assert c2.tile_bytes(FP1, 1) is None and c2.tile_bytes(FP1, 1, Q.STANDARD) is None
    db.close()


@pytest.mark.parametrize("q", Q.PRESETS, ids=lambda q: q.name)
def test_sheet_geometry_per_quality(cache, q):
    n = q.per_sheet + 2
    for k in range(n):
        cache.put_tile(FP1, k, k * 10_000, jpeg(q.w, q.h, ((k * 37) % 256, 0, 0)), q)
    data, v = cache.sheet_bytes(FP1, 0, q)
    assert v == q.per_sheet and jpeg_size(data) == q.sheet_size
    with Image.open(io.BytesIO(data)) as im:
        assert im.quantization == qtables(q.jpeg_q)                    # sheet JPEG quality follows the preset
        k = q.per_sheet - 1
        x, y = (k % q.cols) * q.w, (k // q.cols) * q.h
        r = im.convert("RGB").getpixel((x + q.w // 2, y + q.h // 2))[0]
        assert abs(r - (k * 37) % 256) < 12                             # the slot holds tile k
    data, v = cache.sheet_bytes(FP1, 1, q)
    assert v == 2


def test_legacy_database_and_cache_migrate_to_standard(tmp_path):
    """A cache made before quality presets: old `tiles` table, tiles in the Standard folder."""
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript("""
CREATE TABLE entries (fp TEXT PRIMARY KEY, duration_ms INTEGER, n_tiles INTEGER, version INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, last_used REAL NOT NULL);
CREATE TABLE tiles (fp TEXT NOT NULL, k INTEGER NOT NULL, pts_ms INTEGER NOT NULL, size INTEGER NOT NULL,
  created REAL NOT NULL, PRIMARY KEY (fp, k));
""")
    data = jpeg(320, 180)
    tdir = tmp_path / "data" / "cache" / FP1[:2] / FP1 / "t320x180q75g10v1" / "tiles"
    tdir.mkdir(parents=True)
    for k in range(3):
        (tdir / f"{k}.jpg").write_bytes(data)
        con.execute("INSERT INTO tiles VALUES (?,?,?,?,?)", (FP1, k, k * 10_000, len(data), time.time()))
    con.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?)", (FP1, 30_000, 3, 7, 3 * len(data), time.time(), time.time()))
    con.commit()
    con.close()

    db = DB(path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(tiles)")}
    assert "profile" in cols
    c = Cache(tmp_path / "data", db, 1 << 30, 30)
    e = c.entry(FP1)
    assert e.tiles == {0: 0, 1: 10_000, 2: 20_000} and e.version == 7 and e.n_tiles == 3
    assert all(c.tile_bytes(FP1, k) == data for k in range(3))
    assert c.entry(FP1, Q.HIGH).tiles == {}                            # nothing pretends to be High
    # an older server (no profile column in its INSERT) still writes Standard rows after the migration
    db.x("INSERT INTO tiles(fp,k,pts_ms,size,created) VALUES(?,?,?,?,?)", (FP1, 9, 90_000, 1, 0.0))
    assert db.q("SELECT profile FROM tiles WHERE k=9")[0][0] == PROFILE
    db.close()
    DB(path).close()                                                   # migration is idempotent


# ----------------------------------------------------------------------------- VTT
def _xywh(url: str) -> tuple[int, int, int, int]:
    return tuple(int(v) for v in url.split("#xywh=")[1].split(","))


@pytest.mark.parametrize("q", Q.PRESETS, ids=lambda q: q.name)
def test_vtt_coordinates_follow_the_quality(q):
    n = 2 * q.per_sheet + 3
    tiles = {k: k * 10_000 for k in range(n)}
    for layout in ("tiles", "sheets", "hybrid"):
        # n + 5 slots in the title: the last block is incomplete (the final block of a title counts as complete)
        cues = parse_vtt(render("https://h:1", "c" * 43, "p" * 22, 9, layout, tiles, (n + 5) * 10_000, n + 5, 5000,
                                quality=q))
        assert len(cues) == n
        for _a, _b, url in cues:
            qs = parse_qs(urlsplit(url.split("#")[0]).query)
            assert qs.get("q") == (None if q is Q.STANDARD else [q.name])
            k = int(qs["t"][0]) // 10_000
            x, y, w, h = _xywh(url)
            assert (w, h) == (q.w, q.h)
            if "/sheets/" in url:
                n_sheet = int(urlsplit(url).path.rsplit("/", 1)[1][:-4])
                slot = k % q.per_sheet
                assert n_sheet == k // q.per_sheet
                assert (x, y) == ((slot % q.cols) * q.w, (slot // q.cols) * q.h)
                assert x + w <= q.sheet_size[0] and y + h <= q.sheet_size[1]
            else:
                assert (x, y) == (0, 0)
            if layout == "hybrid":
                assert ("/sheets/" in url) == (k < 2 * q.per_sheet)  # only complete blocks become sheets


def test_standard_vtt_is_byte_identical_to_the_original_format():
    tiles = {k: k * 10_000 for k in range(5)}
    text = render("https://h:1", "c" * 43, "p" * 22, 9, "tiles", tiles, 50_000, 5, 5000)
    assert "&q=" not in text and "/tiles/2.jpg?exp=9&t=20000#xywh=0,0,320,180" in text


def test_vtt_fallback_keeps_each_cue_consistent():
    hi = {0: 0, 1: 10_000}
    std = {k: k * 10_000 for k in range(4)}
    cues = parse_vtt(render("https://h:1", "c" * 43, "p" * 22, 9, "hybrid", hi, 40_000, 4, 5000,
                            quality=Q.HIGH, fallback=[(Q.STANDARD, std)]))
    got = {int(parse_qs(urlsplit(u.split("#")[0]).query)["t"][0]) // 10_000: u for _a, _b, u in cues}
    assert sorted(got) == [0, 1, 2, 3]
    for k in (0, 1):
        assert "&q=high" in got[k] and _xywh(got[k])[2:] == (480, 270)
    for k in (2, 3):
        assert "&q=" not in got[k] and _xywh(got[k])[2:] == (320, 180)


# ----------------------------------------------------------------------------- jobs (fake engine)
class SizedEngine:
    """Fake engine that renders at whatever preset the runner asks for (like the real worker)."""

    def __init__(self, delay=0.02, log=None, gate=None):
        self.delay, self.opened, self.thumb = delay, False, None
        self.log = log if log is not None else []
        self.gate = gate

    def is_open(self):
        return self.opened

    def open(self):
        self.opened = True
        return OpenResult(600_000, 640, 360, "h264", False)       # 60 tiles

    def set_thumb(self, wire):
        self.thumb = list(wire)

    def grab(self, t_ms, accurate):
        w, h, q = self.thumb or [320, 180, 75]
        if self.gate is not None:
            self.gate(t_ms)
        time.sleep(self.delay)
        self.log.append((t_ms // 10_000, (w, h)))
        return GrabResult(t_ms, jpeg(w, h, q=q))

    def kill(self):
        self.opened = False

    def close(self):
        self.opened = False


@pytest.fixture()
def mgr(origin, tmp_path):
    made = []

    def make(quality=Q.STANDARD, **engine_kw):
        db = DB(tmp_path / f"m{len(made)}.db")
        cache = Cache(tmp_path / f"d{len(made)}", db, 1 << 30, 30, quality=quality)
        proxy = RangeProxy().start()
        log: list = []
        m = JobManager(Settings().with_(pregen="full", create_wait_s=5.0),
                       make_policy([origin], {HOST: ["127.0.0.1"]}), cache, db, proxy,
                       engine_factory=lambda src: SizedEngine(log=log, **engine_kw))
        m.grabs = log
        m.start()
        made.append((m, proxy, db))
        return m

    yield make
    for m, proxy, db in made:
        m.shutdown()
        proxy.stop()
        db.close()


def _url(origin):
    return f"http://{HOST}:{origin.port}/f/main.mkv"


def test_quality_change_during_generation(origin, mgr):
    gate = threading.Event()
    hold = {"k": None}

    def gated(t_ms):                                   # hold tile 20 in flight while the quality changes
        if t_ms == 200_000 and hold["k"] is None:
            hold["k"] = 20
            gate.wait(10)

    m = mgr(delay=0.005, gate=gated)
    job = m.create("d" * 16, _url(origin), {}, None, 0, "hybrid")
    src = m.sources[job.fp]
    wait_for(lambda: hold["k"] == 20, timeout=20)
    assert m.set_quality(Q.MAX) and src.quality is Q.MAX
    gate.set()
    wait_for(lambda: m.state(job)[0] == "complete", timeout=30)
    std, mx = m.cache.entry(job.fp, Q.STANDARD), m.cache.entry(job.fp, Q.MAX)
    assert len(mx.tiles) == 60                                     # the new quality is complete
    assert 20 in std.tiles                                         # the in-flight frame kept its own quality ...
    assert all(jpeg_size(m.cache.tile_bytes(job.fp, k, Q.STANDARD)) == (320, 180) for k in std.tiles)
    assert all(jpeg_size(m.cache.tile_bytes(job.fp, k, Q.MAX)) == (640, 360) for k in mx.tiles)
    made_max = [k for k, size in m.grabs if size == (640, 360)]
    assert sorted(made_max) == list(range(60))                     # ... and no slot was generated twice at Max
    assert m.snapshot(job)["tiles_done"] == 60


def test_switch_back_reuses_cache_without_regenerating(origin, mgr):
    m = mgr(delay=0.02)
    job = m.create("d" * 16, _url(origin), {}, None, 0, "hybrid")
    wait_for(lambda: m.state(job)[0] == "complete", timeout=30)
    v0 = m.version(job)
    m.set_quality(Q.LOW)
    assert m.version(job) > v0                                     # clients re-read the VTT
    assert m.state(job)[0] == "ready"                              # Standard tiles still shown meanwhile
    wait_for(lambda: m.state(job)[0] == "complete", timeout=30)
    n = len(m.grabs)
    m.set_quality(Q.STANDARD)
    m.priority(job, None, 0)
    time.sleep(0.5)
    assert m.state(job)[0] == "complete" and len(m.grabs) == n      # cached Standard tiles reused, no new work
    assert len(m.cache.entry(job.fp, Q.LOW).tiles) == 60           # and Low was not deleted


def test_install_without_the_setting_generates_standard(origin, mgr):
    """A server that never stored the setting behaves exactly as before: Standard tiles only."""
    m = mgr(delay=0.0)
    job = m.create("d" * 16, _url(origin), {}, None, 0, "hybrid")
    wait_for(lambda: m.state(job)[0] == "complete", timeout=30)
    assert {s for _k, s in m.grabs} == {(320, 180)}


# ----------------------------------------------------------------------------- end to end (real worker, PyAV)
def _policy(origin):
    return make_policy([origin], {HOST: ["127.0.0.1"]})


@pytest.mark.parametrize("name", ["high", "max"])
def test_real_output_dimensions_quality_and_timestamps(origin, live_factory, name):
    q = Q.BY_NAME[name]
    srv = live_factory(_policy(origin))
    with srv.admin_client() as a:
        r = a.put("/admin/thumbnails", json={"quality": name})
        assert r.status_code == 200 and r.json()["quality"] == name
        assert a.get("/admin/metrics").json()["config"]["thumb_quality"] == name
    token, _ = srv.pair()
    with srv.client(token) as c:
        body = c.post("/v1/previews", json={"source": {"url": _url(origin)}, "playhead_ms": 0, "layout": "hybrid"}).json()
        pid = body["preview_id"]
        st = wait_for(lambda: (s := c.get(f"/v1/previews/{pid}").json())["state"] == "complete" and s, timeout=90)
        cues = parse_vtt(c.get(st["vtt_url"]).text)
    assert len(cues) == 12
    import httpx

    with httpx.Client(verify=False) as anon:
        for a_ms, b_ms, url in cues:
            assert f"&q={name}" in url
            t = int(url.split("&t=")[1].split("#")[0])
            assert a_ms <= t < b_ms
            x, y, w, h = _xywh(url)
            r = anon.get(url.split("#")[0])
            assert r.status_code == 200
            with Image.open(io.BytesIO(r.content)) as im:
                assert im.quantization == qtables(q.jpeg_q)
                if "/sheets/" in url:
                    assert x + w <= im.size[0] and y + h <= im.size[1] and (w, h) == (q.w, q.h)
                    b = io.BytesIO()
                    im.crop((x, y, x + w, y + h)).save(b, "JPEG", quality=95)
                    tile = b.getvalue()
                else:
                    assert im.size == (q.w, q.h) == (w, h) and (x, y) == (0, 0)
                    tile = r.content
            frame = decode_barcode(tile, (0, 0, q.w, q.h))
            assert frame is not None and abs(frame * 1000 / FPS - t) <= 1000 / FPS + 1   # seek accuracy kept
            # the tile endpoint without q= is the Standard one, which this title never had
            k = t // 10_000
            plain = url.split("/sheets/")[0].split("/tiles/")[0] + f"/tiles/{k}.jpg?exp=" + url.split("exp=")[1].split("&")[0]
            assert anon.get(plain).status_code == 404
            assert anon.get(plain + "&q=ultra").status_code == 404


def test_admin_api_validates_and_persists(origin, live_factory, tmp_path):
    d = tmp_path / "persist"
    srv = live_factory(_policy(origin), data_dir=d)
    with srv.admin_client() as a:
        g = a.get("/admin/thumbnails").json()
        assert g["quality"] == "standard" and g["default"] == "standard"
        assert [p["name"] for p in g["presets"]] == ["low", "standard", "high", "max"]
        assert a.put("/admin/thumbnails", json={"quality": "ultra"}).status_code in (400, 422)
        assert a.put("/admin/thumbnails", json={}).status_code in (400, 422)
        assert a.get("/admin/thumbnails").json()["quality"] == "standard"
        assert a.put("/admin/thumbnails", json={"quality": "low"}).json()["quality"] == "low"
    srv.stop()
    srv2 = live_factory(_policy(origin), data_dir=d)
    with srv2.admin_client() as a:
        assert a.get("/admin/thumbnails").json()["quality"] == "low"
