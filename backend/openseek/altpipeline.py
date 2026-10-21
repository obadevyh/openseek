"""Alternative-source (low-resolution thumbnail source) pipeline glue. Feature flag: OPENSEEK_ALT_SOURCE.

Flow (all optional; the playback source A keeps producing Priority C tiles throughout):
  1. The client sends candidate METADATA with the preview request (`alt` block).
  2. `AltMeta.next_wanted()` ranks them; the status response asks the client to resolve ONE (`alt.want`).
  3. The client POSTs the resolved link; `AltController.attach()` preflights it through the normal SSRF
     policy, validates its timeline against A with `timeline.verify_alignment` (frames from both sources),
     and only on acceptance records provenance and switches extraction.
  4. `SwitchEngine` (the engine the runner already uses) then extracts from B at mapped times, converts the
     decoded frame's PTS back to A's timeline, and returns that, so the runner's existing +-tolerance check,
     accurate fallback and cache writes stay unchanged. Any B failure falls back to A inline.
Tiles are stored under A's fingerprint; B's fingerprint and the mapping live only in `thumb_sources`.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import timeline as tl
from .altsource import MAX_ATTEMPTS, Candidate, rank
from .fetch import Budget, Upstream, UpstreamError
from .worker import GrabResult, WorkerEngine, WorkerError

log = logging.getLogger("openseek.alt")

SPOT_SLOTS = 6                   # closed-loop check after generation
SPOT_MIN_CORR = 0.50
SPOT_MIN_CHECKED = 3             # need at least this many comparisons before judging
SPOT_MAX_FAILS = 1               # more failures than this => the alternative tiles are dropped


@dataclass
class AltMeta:
    """Per-job, memory only (titles/ids are never persisted)."""
    play: Candidate
    cands: list[Candidate]
    telemetry: bool = False
    tried: set = field(default_factory=set)
    state: str = "wanted"        # wanted | validating | active | rejected | failed | none
    reason: str = ""
    validation: dict = field(default_factory=dict)
    gain: dict = field(default_factory=dict)
    attempts: list = field(default_factory=list)   # per-attempt request accounting (no URLs)
    hold: bool = False              # a candidate was rejected: wait for an explicit release before the next offer
    cooldown_until: float = 0.0     # a provider rate limit: do not offer another candidate before this (monotonic)
    spot: dict = field(default_factory=dict)

    def next_wanted_any(self) -> str | None:
        """Best untried candidate regardless of the current state (used to re-evaluate a skipped negotiation)."""
        if len(self.tried) >= MAX_ATTEMPTS:
            return None
        for c in rank(self.play, self.cands):
            if c.cid not in self.tried:
                return c.cid
        return None

    def next_wanted(self) -> str | None:
        return self.next_wanted_any() if self.state == "wanted" else None


@dataclass
class AltState:
    cid: str
    mapping: tl.Mapping
    thumb_fp: str
    upstream: Upstream
    proxy_src: object
    cap: str
    host: str
    fmt: str
    size: int
    tol_b: int
    since: float = 0.0               # when it was activated (monotonic seconds): telemetry grace starts here
    fails: int = 0                   # consecutive B failures (circuit breaker)
    active: bool = True


def _sig_grabber(eng: WorkerEngine, counter: list, on_frame=None):
    def grab(t_ms: int):
        res = eng.grab(int(t_ms), accurate=False)
        counter[0] += 1
        if not res.jpeg:                                  # keyframe outside the tolerance: no frame, skip this anchor
            return res.pts_ms, None
        if on_frame is not None:
            on_frame(int(t_ms), res.pts_ms, res.jpeg)
        return res.pts_ms, tl.signature(res.jpeg)
    return grab


def validate(mgr, src, cap_b: str, size_b: int, fmt_b: str, dur_a_ms: int) -> tl.Verdict:
    """Probe both sources with a small pool of temporary workers (validation is round-trip bound, so independent
    anchors run in parallel); returns the timeline verdict. Closes every worker it opened."""
    s = mgr.settings
    pool_n = max(1, int(s.alt_probe_conc))
    engines_a: list[WorkerEngine] = []
    engines_b: list[WorkerEngine] = []
    reused = [0]
    lock = threading.Lock()
    quality = src.quality                                   # validation reads render at the source's preset

    def keep_tile(t_req: int, pts: int, jpeg: bytes) -> None:
        """A validation read is also a finished tile when it lands within tolerance of its slot: store it so the
        playback source never has to fetch that slot again (no wasted request)."""
        k = int(round(t_req / 10_000))
        sch = src.scheduler
        if sch is None or not 0 <= k < sch.n or abs(pts - k * 10_000) > s.tolerance_ms or k in sch.done:
            return
        try:
            mgr.cache.put_tile(src.fp, k, pts, jpeg, quality)
        except (ValueError, OSError):
            return
        with src.cond:
            if src.scheduler is not sch:                    # the quality changed meanwhile
                return
            sch.mark_done(k)
            src.cond.notify_all()
        with lock:
            reused[0] += 1

    def mk(url_cap: str, size: int, fmt: str) -> WorkerEngine:
        eng = WorkerEngine(mgr.proxy.url(url_cap), size, fmt, s.open_timeout_s, s.frame_timeout_s,
                           s.max_dimension, s.max_duration_s, 5000)
        eng.set_thumb(quality.wire())
        return eng
    try:
        for _ in range(pool_n):
            engines_a.append(mk(src.proxy_cap, src.size, src.fmt))
            engines_b.append(mk(cap_b, size_b, fmt_b))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=2 * pool_n) as ex:       # opening a worker costs a process start
            infos = list(ex.map(lambda e: e.open(), engines_a + engines_b))
        ia, ib = infos[0], infos[pool_n]
        kfa, kfb = engines_a[0].keyframes(), engines_b[0].keyframes()
        samplers = []
        for ea, eb in zip(engines_a, engines_b):
            ca, cb = [0], [0]

            def exact_b(t_ms: int, eb=eb):
                res = eb.grab(int(t_ms), accurate=True)
                return res.pts_ms, tl.signature(res.jpeg)
            samplers.append(tl.Sampler(_sig_grabber(ea, ca, keep_tile), _sig_grabber(eb, cb), exact_b))
        v = tl.verify_alignment(ia.duration_ms, ib.duration_ms, kfs_a=kfa or None, kfs_b=kfb or None,
                                slot_ms=10_000, samplers=samplers)
        v.detail = (v.detail + f" a_tiles_kept={reused[0]} probe_workers={pool_n}").strip()
        return v
    finally:
        for e in engines_a + engines_b:
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass


class SwitchEngine:
    """Drop-in for WorkerEngine: A until an alternative source is active, then B at mapped times.

    Safety rules (from review): playback comes first, so a rebuffering/stale/over-budget state WAITS (never pushes);
    a Retry-After on B sends tiles to the playback source (Priority C, which has its own caps) instead of stalling;
    lost telemetry ends the negotiation; repeated B failures deactivate it; urgent work bypasses only the
    concurrency cap, never a pause or a rebuffer."""

    TELEMETRY_GRACE_S = 30.0       # never heard from the client this long after activation => give up on B
    MAX_B_FAILS = 3

    def __init__(self, mgr, src, make_a):
        self.mgr, self.src = mgr, src
        self.a: WorkerEngine = make_a(src)
        self.b: WorkerEngine | None = None
        self._b_for: str | None = None
        self.thumb = None

    # ---- WorkerEngine surface used by the runner
    @property
    def pid(self):
        return (self.b.pid if self.b is not None and self.b.pid else None) or self.a.pid

    def is_open(self) -> bool:
        return self.a.is_open()

    def open(self):
        return self.a.open()

    def kill(self) -> None:
        for e in (self.a, self.b):
            if e is not None:
                e.kill()

    def close(self) -> None:
        for e in (self.a, self.b):
            if e is not None:
                e.close()

    def grab_batch(self, targets_ms, tolerance_ms: int = 5000):
        return self.a.grab_batch(targets_ms, tolerance_ms)

    def set_thumb(self, wire) -> None:
        self.thumb = wire
        for e in (self.a, self.b):
            if e is not None:
                e.set_thumb(wire)

    def keyframe_index(self):
        return self.a.keyframe_index()

    # ---- routing
    def _alt(self) -> AltState | None:
        a = self.src.alt
        return a if a is not None and a.active else None

    def _b_engine(self, alt: AltState) -> WorkerEngine:
        if self.b is None or self._b_for != alt.cid or not self.b.is_open():
            if self.b is not None:
                self.b.close()
            s = self.mgr.settings
            self.b = WorkerEngine(self.mgr.proxy.url(alt.cap), alt.size, alt.fmt, s.open_timeout_s, s.frame_timeout_s,
                                  s.max_dimension, s.max_duration_s, alt.tol_b, nearest_kf=True)
            self.b.set_thumb(self.thumb)
            self.b.open()
            self._b_for = alt.cid
        return self.b

    def _gate(self, alt: AltState, urgent: bool) -> tuple[str, bool]:
        """("b"|"a"|"stop", acquired). Waits while playback needs the connection; see the class docstring."""
        gov = self.src.governor
        clock = self.mgr.clock
        while not self.src.stop_flag:
            if self.src.alt is not alt or not alt.active:
                return "a", False
            st = gov.status(alt.host)
            if st == "none" and clock() - alt.since > self.TELEMETRY_GRACE_S:
                st = "lost"
            if st == "lost":
                log.info("alt telemetry lost; returning to the playback source")
                self.mgr.alt_deactivate(self.src, "telemetry_lost")
                return "a", False
            if st == "pause":
                return "a", False                  # B is rate-limited: Priority C carries on, B stays quiet
            if st == "ok" or (st == "full" and urgent):
                if urgent:
                    return "b", False              # bypasses the concurrency cap only
                if gov.try_acquire(alt.host):
                    return "b", True
            time.sleep(0.05)                       # rebuffer | stale | none (in grace) | full: playback first
        return "stop", False

    def grab(self, t_ms: int, accurate: bool, tolerance_ms: int | None = None) -> GrabResult:
        alt = self._alt()
        if alt is None:
            return self.a.grab(t_ms, accurate, tolerance_ms)
        urgent = bool(self.src.urgent)
        mode, acquired = self._gate(alt, urgent)
        if mode == "stop":
            raise WorkerError("crash", "stopped", transient=True)
        if mode == "a":
            return self.a.grab(t_ms, accurate, tolerance_ms)
        gov = self.src.governor
        t0 = time.perf_counter()
        ok = False
        try:
            alt.proxy_src.last_error = None                # never inherit another tile's error state
            tb = int(round(alt.mapping.to_b(t_ms)))
            eng = self._b_engine(alt)
            res = eng.grab(tb, accurate, alt.tol_b)
            if not res.jpeg and not accurate:              # nearest keyframe outside tol_b: decode forward instead
                res = eng.grab(tb, True, alt.tol_b)
            if not res.jpeg:
                raise WorkerError("internal", "empty_frame")
            ok = True
            alt.fails = 0
            pts_a = int(round(alt.mapping.to_a(res.pts_ms)))
            self.src.alt_slots.add(int(round(t_ms / 10_000)))
            return GrabResult(pts_a, res.jpeg, res.requests, res.bytes, "b")
        except (WorkerError, UpstreamError) as e:
            self._note_failure(alt, e)
            if self.src.stop_flag:
                raise
            return self.a.grab(t_ms, accurate, tolerance_ms)      # this tile from the playback source
        finally:
            if acquired:
                gov.release(alt.host)
            gov.completed(time.perf_counter() - t0, ok)

    def _note_failure(self, alt: AltState, e: Exception) -> None:
        err = getattr(alt.proxy_src, "last_error", None)
        ra = getattr(err, "retry_after", None)
        if ra is not None:
            self.src.governor.retry_after(alt.host, ra)
        reason = getattr(err, "reason", "") or getattr(e, "code", "")
        fatal = err is not None and getattr(err, "code", "") in ("source_expired", "range_unsupported")
        alt.fails += 1
        if fatal or (alt.fails >= self.MAX_B_FAILS and ra is None):
            log.warning("alt source failing (%s); returning to the playback source", reason)
            self.mgr.alt_deactivate(self.src, f"failed:{reason}")
        else:
            log.info("alt grab failed (%s); using the playback source for this tile", reason)


def spot_check(mgr, src, ks: list[int]) -> tuple[int, int]:
    """Closed loop after the grid completes: each stored tile vs a fresh frame of A at the tile's OWN timestamp.
    Returns (checked, failed). A failure on one slot is skipped, never raised; stops promptly with the source."""
    s = mgr.settings
    ea = WorkerEngine(mgr.proxy.url(src.proxy_cap), src.size, src.fmt, s.open_timeout_s, s.frame_timeout_s,
                      s.max_dimension, s.max_duration_s, 5000)
    checked = failed = 0
    try:
        ea.open()
        entry = mgr.cache.entry(src.fp)
        for k in ks:
            if src.stop_flag:
                break
            tile = mgr.cache.tile_bytes(src.fp, k)
            pts = entry.tiles.get(k) if entry is not None else None
            if tile is None or pts is None:
                continue
            try:
                res = ea.grab(int(pts), accurate=False)
                if not res.jpeg:                        # long GOP on A: decode forward to the tile's time
                    res = ea.grab(int(pts), accurate=True)
            except (WorkerError, UpstreamError):
                continue
            sa = tl.signature(res.jpeg) if res.jpeg else None
            sb = tl.signature(tile)
            if sa is None or sb is None:
                continue
            checked += 1
            if tl.correlation(sa, sb) < SPOT_MIN_CORR:
                failed += 1
    finally:
        ea.close()
    return checked, failed
