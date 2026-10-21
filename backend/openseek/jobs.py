"""Jobs, sources and the per-source runner.

* A **job** is what a client sees: `preview_id`, one per (device, content fingerprint).
* A **source** is one piece of content (fingerprint). Tiles are shared across devices through
  it; it owns at most ONE worker subprocess and has at most one frame in flight.
* The runner thread of a source pulls tasks from its `Scheduler`, drives the worker, applies
  the ±tolerance rule (keyframe first; accurate re-extract if the keyframe is too far), publishes
  tiles to the cache, and recovers from worker crashes/timeouts with backoff.
* A source generates at ONE thumbnail quality (the owner's preset). When the preset changes, its
  scheduler is rebuilt from the tiles already cached at the new quality (nothing is deleted, the
  worker keeps running); a frame already in flight is stored under the quality it was rendered at.
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import bisect
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import timeline as tl
from .altpipeline import SPOT_MAX_FAILS, SPOT_MIN_CHECKED, AltMeta, AltState, SwitchEngine, spot_check, validate
from .altsource import Candidate, explain
from .cache import Cache
from .config import Settings
from .db import DB
from .governor import Governor, Limits
from .guard import GuardLimits, PlaybackGuard
from .fetch import Budget, Preflight, ProxySource, RangeProxy, TokenBucket, Upstream, UpstreamError
from .ids import new_preview_id
from .netpolicy import Denied, DestinationPolicy
from .quality import Quality
from .scheduler import Scheduler, Task, tile_count
from .worker import WorkerEngine, WorkerError

ALT_RATE_WAIT_S = 2.5          # how long to wait for a measured playback-source rate before assuming it is slow
log = logging.getLogger("openseek.jobs")

FATAL_UPSTREAM = {"source_expired", "budget_exceeded", "range_unsupported", "source_forbidden"}
MAX_CRASHES = 6               # consecutive worker crashes/timeouts before the source fails
MAX_TILE_FAILURES = 3         # failures of one tile before it is skipped permanently


class Busy(Exception):
    pass


class SourceForbidden(Exception):
    pass


@dataclass
class Job:
    preview_id: str
    device_id: str
    layout: str
    url_key: str
    created: float
    last_activity: float
    fp: str | None = None
    error: str | None = None
    deleted: bool = False
    announced: bool = False
    diagnostic_host: str | None = None    # memory only; loopback diagnostics
    merged_into: "Job | None" = None
    pending_priority: tuple[int | None, int | None] = (None, None)
    alt: "AltMeta | None" = None         # memory only: candidate metadata + alt-source state
    hints: dict | None = None            # memory only: ids from the client; recorded only with the owner's opt-in
    contributed: bool = False            # a guest Contributor's job (restricted policy, own bucket, preempted by the owner)
    policy: object | None = None         # memory only: the ContributionPolicy for such a job
    done_event: threading.Event = field(default_factory=threading.Event)

    def resolve(self) -> "Job":
        j = self
        while j.merged_into is not None:
            j = j.merged_into
        return j


@dataclass
class TileEvent:
    k: int
    cls: str
    pts_ms: int
    at: float
    accurate: bool
    seconds: float
    requests: int = 0
    bytes: int = 0
    keyframe_seconds: float = 0.0
    accurate_seconds: float = 0.0
    throttle_seconds: float = 0.0
    via_alt: bool = False


class SharedShare:
    """Pacer for guest-contributed reads. It always draws from the SAME global background bucket as the owner (so the total never
    exceeds bg_mbps) and, only while the owner is busy, from a second, smaller bucket: work-conserving, owner first."""

    def __init__(self, shared, floor, busy):
        self.shared, self.floor, self.busy = shared, floor, busy

    def throttle(self, n, active, urgent=None):
        self.shared.throttle(n, active, urgent)
        if self.busy():
            self.floor.throttle(n, active, urgent)


class Source:
    def __init__(self, mgr: "JobManager", fp: str):
        self.mgr = mgr
        self.fp = fp
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.upstream: Upstream | None = None
        self.proxy_src: ProxySource | None = None
        self.proxy_cap: str | None = None
        self.fmt: str | None = None
        self.size: int | None = None
        self.scheduler: Scheduler | None = None
        self.kf_pts: list[int] = []                # multi-range prefetch: keyframe times (ms) ...
        self.kf_pos: list[int] = []                # ... and their byte positions, from the container index
        self.kf_block_starts: set[int] = set()
        self.mr_blocks = self.MR_WINDOW_BLOCKS
        self.error: str | None = None
        self.jobs: set[str] = set()
        self.thread: threading.Thread | None = None
        self.extra_threads: list[threading.Thread] = []
        self.stop_flag = False
        self.n_workers = max(1, int(mgr.settings.workers_per_source))
        self.engines: list = [None] * self.n_workers
        self.active_tasks: dict[int, Task] = {}
        self.alt: AltState | None = None
        self.alt_slots: set[int] = set()          # slots whose tile came from the alternative source
        self.last_seek_at = -1e9                  # last scrub / playhead jump seen (monotonic seconds)
        self._pl_last: float | None = None
        self._pl_t = 0.0
        k = mgr.settings.guard_time_scale if mgr.settings.playback_guard else 1.0
        self.governor = Governor(Limits(max_conc=max(1, min(mgr.settings.alt_max_conc, mgr.settings.workers_per_source)),
                                        max_rps=mgr.settings.alt_rps, telemetry_stale_s=10.0 * k, telemetry_lost_s=30.0 * k,
                                        resume_after_s=8.0 * k), clock=mgr.clock)
        self.guard: PlaybackGuard | None = None
        if mgr.settings.playback_guard:
            self.guard = PlaybackGuard(self.governor, self._stall_seconds, mgr.clock,
                                       GuardLimits(stall_s=mgr.settings.guard_stall_s).scaled(k))
        self.background = False
        self.contributed = False
        self.urgent = False
        self.last_activity = mgr.clock()
        self.created = mgr.clock()
        self.events: list[TileEvent] = []
        self.restarts = 0
        self.pending_priority: tuple[int | None, int | None] | None = None
        self._tile_failures: dict[int, int] = {}
        self._crashes = 0
        self._unreachable = 0
        self._priority: tuple[int | None, int | None] = (None, None)
        self.quality: Quality = mgr.quality       # the preset this source generates
        e = mgr.cache.entry(fp, self.quality)
        if e is not None and e.n_tiles:
            self.scheduler = mgr.new_scheduler(e.n_tiles, set(e.tiles))

    # ---------------------------------------------------------------- state
    @property
    def engine(self):
        return self.engines[0] if self.engines else None

    @engine.setter
    def engine(self, val) -> None:
        if not self.engines:
            self.engines = [val]
        else:
            self.engines[0] = val

    @property
    def current(self) -> Task | None:
        return next(iter(self.active_tasks.values()), None)

    @current.setter
    def current(self, val: Task | None) -> None:
        if val is None:
            self.active_tasks.pop(0, None)
        else:
            self.active_tasks[0] = val

    @property
    def running(self) -> bool:
        return (self.thread is not None and self.thread.is_alive()) or any(
            t.is_alive() for t in self.extra_threads
        )

    @property
    def worker_pid(self) -> int | None:
        eng = self.engine
        return getattr(eng, "pid", None) if eng is not None else None

    def touch(self) -> None:
        self.last_activity = self.mgr.clock()

    def _sync_background(self) -> None:
        if not self.active_tasks:
            self.background = False
            self.urgent = bool(self.scheduler is not None and self.scheduler.has_p0())
        else:
            self.background = all(t.cls == "P2" or t.bg for t in self.active_tasks.values())
            self.urgent = any(t.cls == "P0" for t in self.active_tasks.values()) or bool(
                self.scheduler is not None and self.scheduler.has_p0()
            )

    def _stall_seconds(self) -> float:
        up = self.upstream
        return up.stats.stall_seconds() if up is not None else 0.0

    # ---------------------------------------------------------------- multi-range prefetch (default off)
    MR_BLOCK = 256 * 1024
    MR_WINDOW_BLOCKS = 5          # default: covers the reader's 4- and 5-block first reads on high-bitrate files

    def _attach_planner(self) -> None:
        ps = self.proxy_src
        if ps is None or not self.mgr.settings.multirange_prefetch or not self.kf_pts:
            return
        ps.planner = self._plan_windows
        ps.cancelled = lambda: self.stop_flag

    def _load_kf_index(self, eng, duration_ms: int = 0) -> None:
        """Worker 0 reads the container's keyframe index once (it loads the Cues; one extra seek in the worker)."""
        if not self.mgr.settings.multirange_prefetch or self.kf_pts:
            return
        if duration_ms and self.size:                      # the reader's first read: seek_blocks (+1 past half a block)
            bps = self.size / (duration_ms / 1000.0)
            self.mr_blocks = 5 if bps >= 2_500_000 else 3 if bps >= 200_000 else 2
        else:
            self.mr_blocks = self.MR_WINDOW_BLOCKS
        try:
            idx = eng.keyframe_index()
        except (WorkerError, AttributeError):
            return
        if len(idx) < 2:
            return
        with self.cond:
            self.kf_pts = [t for t, _ in idx]
            self.kf_pos = [p for _, p in idx]
            self.kf_block_starts = {(p // self.MR_BLOCK) * self.MR_BLOCK for p in self.kf_pos}
            self._attach_planner()

    def _window_for(self, t_ms: int) -> tuple[int, int] | None:
        """The bytes the reader will ask for first when it seeks to the keyframe at or before t_ms."""
        i = bisect.bisect_right(self.kf_pts, t_ms) - 1
        if i < 0 or self.size is None:
            return None
        start = (self.kf_pos[i] // self.MR_BLOCK) * self.MR_BLOCK
        end = min(self.size, start + self.mr_blocks * self.MR_BLOCK) - 1
        return (start, end) if end >= start else None

    def _plan_windows(self, start: int, end: int) -> list[tuple[int, int]]:
        """Called by the proxy for a background keyframe read: the windows of the next tiles worth fetching with it."""
        if start not in self.kf_block_starts or self.scheduler is None or self.proxy_src is None:
            return []
        if self.guard is not None and self.guard.reason is not None:
            return []
        n_extra = self.mgr.settings.multirange_batch - 1
        with self.cond:
            tasks = self.scheduler.lookahead(n_extra + 2)
        out: list[tuple[int, int]] = []
        for t in tasks:
            if t.cls == "P0" or len(out) >= n_extra:
                continue                                   # urgent scrub targets always use their own single request
            w = self._window_for(t.t_ms)
            if w is None or w[0] <= end and start <= w[1]:
                continue
            if not self.proxy_src.has_or_pending(*w) and w not in out:
                out.append(w)
        return out

    def offer_upstream(self, up: Upstream, pre: Preflight) -> bool:
        """Adopt a freshly preflighted upstream unless a runner is already using a working one."""
        with self.lock:
            if self.upstream is not None and self.running:
                return False
            old_cap, old_up = self.proxy_cap, self.upstream
            self.upstream, self.fmt, self.size = up, pre.fmt, pre.size
            up.budget.urgent = lambda: self.urgent
            self.proxy_src = ProxySource(up, pre.size, [(0, pre.head), (pre.tail_offset, pre.tail)],
                                         background=(lambda: True) if self.contributed else (lambda: self.background),
                                         bucket=self.mgr.contrib_share if self.contributed else self.mgr.bucket,
                                         shared_cache=((self.n_workers > 1 or self.mgr.settings.hierarchical
                                                        or self.mgr.settings.multirange_prefetch)
                                                       and pre.size >= 16 * 1024 * 1024))
            self._attach_planner()
            self.proxy_cap = self.mgr.proxy.register(self.proxy_src)
            if self.error in ("source_expired", "source_unreachable"):
                self.error = None      # a fresh link revives a source whose link had expired
        if old_cap:
            self.mgr.proxy.unregister(old_cap)
        if old_up is not None:
            old_up.close()
        return True

    def set_priority(self, at_ms: int | None, playhead_ms: int | None) -> None:
        with self.cond:
            now = self.mgr.clock()
            if at_ms is not None:
                self.last_seek_at = now                  # scrubbing
            if playhead_ms is not None:
                if self._pl_last is not None and abs(playhead_ms - (self._pl_last + (now - self._pl_t) * 1000.0)) > 4000:
                    self.last_seek_at = now              # the playhead jumped: a skip or a seek
                self._pl_last, self._pl_t = playhead_ms, now
            self._priority = (at_ms, playhead_ms)
            if self.scheduler is None:
                self.pending_priority = (at_ms, playhead_ms)
            else:
                self.scheduler.set_priority(at_ms, playhead_ms)
            if at_ms is not None:
                self.background = False          # let an in-flight P2 frame finish at full speed
                self.urgent = True
            else:
                self._sync_background()
            self.cond.notify_all()

    def apply_quality(self, q: Quality) -> bool:
        """Generate at `q` from now on. The scheduler restarts from the tiles cached at `q` (none are deleted);
        frames already in flight finish and are stored under their own quality. True when something changed."""
        with self.cond:
            if q == self.quality:
                return False
            self.quality = q
            if self.scheduler is not None:
                e = self.mgr.cache.entry(self.fp, q)
                self.scheduler = self.mgr.new_scheduler(self.scheduler.n, set(e.tiles) if e else set())
                if self._priority != (None, None):
                    self.scheduler.set_priority(*self._priority)
            self._tile_failures.clear()
            self._sync_background()
            self.cond.notify_all()
        log.info("source %s.. now generating %s thumbnails", self.fp[:8], q.name)
        return True

    def tile_lost(self, k: int) -> None:
        with self.cond:
            if self.scheduler is not None:
                self.scheduler.mark_missing(k)
            self.cond.notify_all()

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        self.apply_quality(self.mgr.quality)
        with self.lock:
            if self.running or self.error is not None or self.upstream is None:
                return False
            if self.scheduler is not None and self.scheduler.complete_all:
                return False
            self.stop_flag = False
            self._crashes = 0
            self._unreachable = 0
            self.extra_threads = []
            self.thread = threading.Thread(target=self._run, args=(0,),
                                           name=f"openseek-src-{self.fp[:8]}-0", daemon=True)
            self.thread.start()
            return True

    def _ensure_extra_workers(self) -> None:
        with self.cond:
            if self.n_workers <= 1 or self.stop_flag or self.error is not None:
                return
            if self.scheduler is None or self.scheduler.complete_all or self.scheduler.n <= 1:
                return
            alive_extra = [t for t in self.extra_threads if t.is_alive()]
            if alive_extra:
                return
            self.extra_threads = []
            for idx in range(1, self.n_workers):
                t = threading.Thread(target=self._run, args=(idx,),
                                     name=f"openseek-src-{self.fp[:8]}-{idx}", daemon=True)
                self.extra_threads.append(t)
                t.start()

    def stop(self, reason: str, wait: bool = True) -> None:
        """Stop the runner and kill all workers immediately (in-flight frames are abandoned)."""
        with self.cond:
            self.stop_flag = True
            engs = [e for e in self.engines if e is not None]
            threads = ([self.thread] if self.thread is not None else []) + list(self.extra_threads)
            self.cond.notify_all()
        for eng in engs:
            eng.kill()
        cur = threading.current_thread()
        if wait:
            for t in threads:
                if t is not None and t is not cur:
                    t.join(timeout=10)
        if self.upstream is not None:
            self.upstream.drop_connection()
        if self.alt is not None:
            self.mgr.alt_deactivate(self, "stopped", final=False)
        log.info("source %s.. stopped (%s)", self.fp[:8], reason)

    def fail(self, code: str) -> None:
        with self.cond:
            if self.error is None:
                self.error = code
                log.warning("source %s.. failed: %s", self.fp[:8], code)
            self.stop_flag = True
            self.cond.notify_all()
        self.mgr.bump()

    # ---------------------------------------------------------------- runner
    def _upstream_error(self) -> str | None:
        ps = self.proxy_src
        return ps.last_error.code if ps is not None and ps.last_error is not None else None

    def _finish(self, worker_idx: int, task: Task | None, ok: bool, permanent: bool = False) -> None:
        with self.cond:
            self.active_tasks.pop(worker_idx, None)
            self._sync_background()
            # a task of a scheduler replaced meanwhile (quality change) must not mark anything in the new one
            if task is not None and self.scheduler is not None and self.scheduler.inflight_tasks.get(task.k) is task:
                self.scheduler.complete(task, ok=ok, permanent_failure=permanent)
                if ok and self.guard is not None and task.cls != "P0":
                    self.guard.note_tile_done()
            self.cond.notify_all()

    def _next_task(self, worker_idx: int) -> tuple[bool, Task | None]:
        """(keep_running, task). Blocks briefly while there is nothing to do."""
        with self.cond:
            if self.stop_flag or self.error is not None:
                return False, None
            if self.scheduler is None:
                if worker_idx == 0:
                    return True, None                   # worker 0 opens the engine to learn duration
                self.cond.wait(timeout=0.2)
                return True, None
            if self.guard is not None and not self.scheduler.has_p0():
                # playback first: background work (P1/P2) waits while the guard is holding it; urgent scrub targets never do
                if self.guard.check() is not None or not self.guard.worker_slot_free(len(self.active_tasks)):
                    self.cond.wait(timeout=0.5)
                    return True, None
            if self.contributed and (self.mgr.owner_hold() or (worker_idx > 0 and self.mgr.owner_busy())):
                self.cond.wait(timeout=0.5)       # held while the owner struggles; down to ONE worker while the owner is busy
                return True, None
            task = self.scheduler.next()
            if task is None:
                if self.scheduler.complete_all:
                    return False, None
                self.cond.wait(timeout=min(1.0, max(0.05, self.scheduler.seconds_until_p2() or 1.0)))
                return True, None
            self.active_tasks[worker_idx] = task
            self._sync_background()
            return True, task

    def _run(self, worker_idx: int = 0) -> None:
        mgr = self.mgr
        tol = mgr.settings.tolerance_ms
        try:
            if worker_idx == 0 and self.scheduler is not None:
                self._ensure_extra_workers()
            while True:
                keep, task = self._next_task(worker_idx)
                if not keep:
                    if worker_idx == 0 and self.alt is not None and self.scheduler is not None \
                            and self.scheduler.complete_all and not self.stop_flag:
                        mgr.alt_spot_check(self)
                    return
                if self.scheduler is not None and task is None:
                    continue
                eng = self.engines[worker_idx]
                if eng is None or not eng.is_open():
                    with self.cond:
                        crashes_now = self._crashes
                    if crashes_now:
                        with self.cond:   # restart backoff: 0.5 s, 1 s, 2 s ... capped at 8 s
                            self.cond.wait(timeout=min(8.0, 0.25 * (2 ** crashes_now)))
                    if self.stop_flag or not self._open_engine(worker_idx):
                        self._finish(worker_idx, task, ok=False)
                        if self.error is not None or self.stop_flag:
                            return
                        with self.cond:
                            self._crashes += 1
                            self.restarts += 1
                            crashes_now = self._crashes
                        if crashes_now >= MAX_CRASHES:
                            self.fail("internal")
                            return
                        continue
                    eng = self.engines[worker_idx]
                if task is None:
                    continue
                if self.stop_flag:
                    self._finish(worker_idx, task, ok=False)
                    return
                t0 = time.perf_counter()
                if self.proxy_src is not None:
                    self.proxy_src.last_error = None
                accurate = False
                budget = self.upstream.budget
                requests_before, bytes_before = budget.requests, budget.used
                throttle_before = self.proxy_src.throttle_seconds
                keyframe_seconds = accurate_seconds = 0.0
                worker_reqs = worker_bytes = 0
                quality = self.quality                     # the tile is rendered (and stored) at this preset
                set_thumb = getattr(eng, "set_thumb", None)
                if set_thumb is not None:
                    set_thumb(quality.wire())
                try:
                    res = eng.grab(task.t_ms, accurate=False)
                    keyframe_seconds = time.perf_counter() - t0
                    worker_reqs += getattr(res, "requests", 0)
                    worker_bytes += getattr(res, "bytes", 0)
                    if abs(res.pts_ms - task.t_ms) > tol:
                        accurate = True     # keyframe too far from the grid point (long GOP)
                        accurate_start = time.perf_counter()
                        res = eng.grab(task.t_ms, accurate=True)
                        accurate_seconds = time.perf_counter() - accurate_start
                        worker_reqs += getattr(res, "requests", 0)
                        worker_bytes += getattr(res, "bytes", 0)
                except WorkerError as e:
                    if self.stop_flag:
                        self._finish(worker_idx, task, ok=False)
                        return
                    up = self._upstream_error()
                    if up in FATAL_UPSTREAM:
                        self._finish(worker_idx, task, ok=False)
                        self.fail(up)
                        return
                    if up == "source_unreachable":
                        with self.cond:
                            self._unreachable += 1
                            unreach_now = self._unreachable
                        if unreach_now >= 3:
                            self._finish(worker_idx, task, ok=False)
                            self.fail("source_unreachable")
                            return
                    with self.cond:
                        n = self._tile_failures.get(task.k, 0) + 1
                        self._tile_failures[task.k] = n
                    self._finish(worker_idx, task, ok=False, permanent=n >= MAX_TILE_FAILURES)
                    if e.code in ("not_video", "unsupported_codec"):
                        self.fail(e.code)
                        return
                    if e.transient:
                        with self.cond:
                            self._crashes += 1
                            self.restarts += 1
                            crashes_now = self._crashes
                        log.warning("worker %s on %s..; restarting (%d)", e.code, self.fp[:8], crashes_now)
                        if crashes_now >= MAX_CRASHES:
                            self.fail("internal")
                            return
                    continue
                with self.cond:
                    self._crashes = 0
                    self._unreachable = 0
                if abs(res.pts_ms - task.t_ms) > tol:
                    self._finish(worker_idx, task, ok=False, permanent=True)   # never publish out of tolerance
                    continue
                try:
                    mgr.cache.put_tile(self.fp, task.k, res.pts_ms, res.jpeg, quality)
                except (ValueError, OSError) as e:
                    log.warning("could not store tile: %s", type(e).__name__)
                    self._finish(worker_idx, task, ok=False, permanent=True)
                    continue
                self._finish(worker_idx, task, ok=True)
                with self.cond:
                    ev_reqs = (budget.requests - requests_before) if self.n_workers == 1 else worker_reqs
                    ev_bytes = (budget.used - bytes_before) if self.n_workers == 1 else worker_bytes
                    self.events.append(TileEvent(task.k, task.cls, res.pts_ms, mgr.clock(), accurate,
                                                 time.perf_counter() - t0,
                                                 ev_reqs,
                                                 ev_bytes,
                                                 keyframe_seconds, accurate_seconds,
                                                 self.proxy_src.throttle_seconds - throttle_before,
                                                 getattr(res, "via", "a") == "b"))
                    event = self.events[-1]
                    log.debug("tile class=%s k=%d seconds=%.3f requests=%d received_bytes=%d "
                              "keyframe_seconds=%.3f accurate_seconds=%.3f",
                              event.cls, event.k, event.seconds, event.requests, event.bytes,
                              event.keyframe_seconds, event.accurate_seconds)
                    if len(self.events) > 5000:
                        del self.events[:1000]
                mgr.bump()
        except Exception:  # noqa: BLE001
            log.exception("runner crashed for %s..", self.fp[:8])
            self.fail("internal")
        finally:
            with self.cond:
                eng, self.engines[worker_idx] = self.engines[worker_idx], None
                self.active_tasks.pop(worker_idx, None)
                self._sync_background()
            if eng is not None:
                eng.close()
            mgr.bump()

    def _open_engine(self, worker_idx: int = 0) -> bool:
        mgr = self.mgr
        eng = mgr.engine_factory(self)
        with self.cond:
            old, self.engines[worker_idx] = self.engines[worker_idx], eng
            if self.stop_flag:
                self.engines[worker_idx] = None
        if old is not None:
            old.close()
        if self.engines[worker_idx] is None:
            return False
        if self.proxy_src is not None:
            self.proxy_src.last_error = None
        try:
            info = eng.open()
        except WorkerError as e:
            up = self._upstream_error()
            if up in FATAL_UPSTREAM:
                self.fail(up)
            elif e.code in ("not_video", "unsupported_codec"):
                self.fail(e.code)
            elif not e.transient and up is not None:
                self.fail(up)
            eng.close()
            with self.cond:
                if self.engines[worker_idx] is eng:
                    self.engines[worker_idx] = None
            return False
        n = tile_count(info.duration_ms)
        with self.cond:
            if self.scheduler is None or self.scheduler.n != n:
                e = mgr.cache.entry(self.fp, self.quality)
                self.scheduler = mgr.new_scheduler(n, set(e.tiles) if e else set())
            if self.pending_priority is not None:
                self.scheduler.set_priority(*self.pending_priority)
                self.pending_priority = None
            self.cond.notify_all()
        mgr.cache.set_media(self.fp, info.duration_ms, n)
        if worker_idx == 0:
            self._load_kf_index(eng, info.duration_ms)
            self._ensure_extra_workers()
        mgr.bump()
        return True


class JobManager:
    library = None                    # set by Core: the opt-in cross-release metadata index

    def size_of(self, fp: str) -> int:
        src = self.sources.get(fp)
        return int(src.size or 0) if src is not None else 0

    def __init__(self, settings: Settings, policy: DestinationPolicy, cache: Cache, db: DB, proxy: RangeProxy,
                 engine_factory: Callable[[Source], object] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.settings = settings
        self.policy = policy
        self.cache = cache
        self.db = db
        self.proxy = proxy
        self.clock = clock
        self.bucket = TokenBucket(settings.bg_mbps * 125_000)
        self.contrib_floor = TokenBucket(settings.contrib_mbps * 125_000)
        self.contrib_share = SharedShare(self.bucket, self.contrib_floor, self.owner_busy)
        self.request_bucket = (TokenBucket(settings.upstream_rps, burst=1)
                               if settings.upstream_rps else None)
        self.alt_validation_s: float | None = None        # last measured validation time (seconds)
        self.alt_resolve_times: deque = deque(maxlen=256)  # monotonic times of alt link resolutions (rate ledger)
        self.alt_bucket = TokenBucket(settings.alt_mbps * 125_000)
        self.alt_request_bucket = TokenBucket(settings.alt_rps, burst=1)
        if settings.alt_shared_caps and self.request_bucket is not None:
            # the playback source and the alternative draw from the SAME request and byte buckets: the total
            # background traffic (validation included) never exceeds the one configured rate
            self.alt_request_bucket = self.request_bucket
            self.alt_bucket = self.bucket
        self.alt_pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="openseek-alt")
        self.engine_factory = engine_factory or self._default_engine
        self.quality: Quality = cache.quality         # the owner's thumbnail preset (see set_quality)
        self.jobs: dict[str, Job] = {}
        self.sources: dict[str, Source] = {}
        self.url_index: dict[tuple[str, str], Job] = {}
        self.reserved = 0
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="openseek-preflight")
        self._version_tick = 0
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="openseek-reaper", daemon=True)
        self._last_evict = 0.0
        cache.on_tile_lost = self._tile_lost

    def start(self) -> None:
        self._reaper.start()

    def _plain_engine(self, src: Source):
        s = self.settings
        return WorkerEngine(self.proxy.url(src.proxy_cap), src.size, src.fmt, s.open_timeout_s, s.frame_timeout_s,
                            s.max_dimension, s.max_duration_s, s.tolerance_ms,
                            nearest_kf=s.nearest_keyframe)

    def _default_engine(self, src: Source):
        if self.settings.alt_source:
            return SwitchEngine(self, src, self._plain_engine)       # A until an alternative source is validated
        return self._plain_engine(src)

    def new_scheduler(self, n: int, done: set[int]) -> Scheduler:
        return Scheduler(n, self.settings.pregen, done, clock=self.clock,
                         p2_pause_s=self.settings.p2_pause_s,
                         p1_interleave=self.settings.p1_interleave,
                         max_inflight=self.settings.workers_per_source,
                         hierarchical=self.settings.hierarchical)

    def bump(self) -> None:
        self._version_tick += 1

    def set_quality(self, q: Quality) -> bool:
        """Switch the thumbnail preset live. Caches of every quality are kept; sources being watched carry on at
        `q` from the tiles already cached at `q`; idle ones pick it up when next started. True if it changed."""
        with self.lock:
            if q == self.quality:
                return False
            self.quality = q
            self.cache.quality = q
            sources = list(self.sources.values())
        self.cache.bump_versions()                  # clients re-read the VTT: it now prefers the new quality
        for src in sources:
            src.apply_quality(q)
            with self.lock:
                owners = [self.jobs[p] for p in src.jobs if p in self.jobs and not self.jobs[p].deleted]
                watched = self.clock() - src.last_activity < self.settings.idle_timeout_s
                if owners and watched and not src.running and not self._stop.is_set():
                    self._ensure_running(src, owners[0].device_id)
        self.bump()
        return True

    def fallback_tiles(self, fp: str) -> list[tuple[Quality, dict[int, int]]]:
        """Tiles of this fingerprint cached at qualities other than the active one, nearest size first (larger
        first on a tie): the VTT uses them for slots the active quality has not made yet."""
        q = self.quality
        others = [o for o in self.cache.cached_qualities(fp) if o != q]
        others.sort(key=lambda o: (abs(o.w - q.w), -o.w))
        out = []
        for o in others:
            e = self.cache.entry(fp, o)
            if e is not None and e.tiles:
                with e.lock:
                    out.append((o, dict(e.tiles)))
        return out

    def _tile_lost(self, fp: str, k: int) -> None:
        """Cache callback (may run under cache locks): hand off to a thread, then regenerate."""
        def work():
            with self.lock:
                src = self.sources.get(fp)
                if src is None:
                    return
            with src.lock:
                completed = src.scheduler is not None and src.scheduler.complete_all
                runners = ([src.thread] if src.thread is not None else []) + list(src.extra_threads)
            src.tile_lost(k)
            # A completed runner may still be closing its decoder. Starting now
            # would see running=True and lose this regeneration wakeup.
            if completed:
                cur = threading.current_thread()
                for r in runners:
                    if r is not None and r is not cur:
                        r.join(timeout=10)
            with self.lock:
                if self._stop.is_set():
                    return
                owners = [self.jobs[p] for p in src.jobs if p in self.jobs]
                if owners:
                    self._ensure_running(src, owners[0].device_id)
        threading.Thread(target=work, name="openseek-regen", daemon=True).start()

    # ---------------------------------------------------------------- capacity
    def running_sources(self) -> list[Source]:
        return [s for s in self.sources.values() if s.running]

    def _device_freeable(self, device_id: str) -> list[Source]:
        """Running sources used only by this device, oldest activity first."""
        out = []
        for s in self.running_sources():
            owners = {self.jobs[j].device_id for j in s.jobs if j in self.jobs}
            if owners == {device_id}:
                out.append(s)
        return sorted(out, key=lambda s: s.last_activity)

    # ---------------------------------------------------------------- create
    def owner_hold(self) -> bool:
        """True while the OWNER's own extraction is held by its playback guard or serving an urgent scrub."""
        for s in list(self.sources.values()):
            if not s.contributed and ((s.guard is not None and s.guard.reason is not None) or (s.running and s.urgent)):
                return True
        return False

    def owner_busy(self) -> bool:
        """The owner is extracting, or playing (fresh playback telemetry on an owner source): contributions step back."""
        for s in list(self.sources.values()):
            if s.contributed:
                continue
            if s.running:
                return True
            age = s.governor.guard_view()["age"]
            if age is not None and age < 15.0:
                return True
        return False

    def forget_source(self, fp: str) -> bool:
        """After the cache entry for `fp` was deleted: drop the idle in-memory source so a new request re-extracts from scratch
        (an idle source would otherwise keep believing its finished scheduler). False if it is running (the caller refuses)."""
        with self.lock:
            src = self.sources.get(fp)
            if src is not None and src.running:
                return False
            if src is None:
                for pid, j in list(self.jobs.items()):
                    if j.fp == fp:
                        j.deleted = True
                        self.jobs.pop(pid, None)
                        self.url_index.pop((j.device_id, j.url_key), None)
                return True
            self.sources.pop(fp, None)
            for pid, j in list(self.jobs.items()):                 # the previews of that title are gone: clients get 404 and ask again
                if j.fp == fp:
                    j.deleted = True
                    self.jobs.pop(pid, None)
                    self.url_index.pop((j.device_id, j.url_key), None)
        if src.proxy_cap:
            self.proxy.unregister(src.proxy_cap)
        if src.upstream is not None:
            src.upstream.close()
        return True

    def resume(self, job: Job) -> bool:
        """A contribution that was paused or preempted continues from the tiles already cached (the scheduler is kept)."""
        j = job.resolve()
        src = self.sources.get(j.fp) if j.fp else None
        with self.lock:
            if (src is None or not src.contributed or src.running or src.error is not None
                    or len(self.running_sources()) + self.reserved >= self.settings.max_active_sources):
                return False
            self._ensure_running(src, j.device_id)
            return src.running

    def contributed_jobs(self) -> list[dict]:
        out = []
        with self.lock:
            jobs = [j for j in self.jobs.values() if j.contributed and not j.deleted and j.merged_into is None]
        for j in jobs:
            snap = self.snapshot(j)
            out.append({"preview_id": j.preview_id, "device_id": j.device_id, "state": snap["state"], "error": snap["error"],
                        "done": snap["tiles_done"], "total": snap["tiles_total"]})
        return out

    def stop_contributions(self, device_id: str | None = None, preview_id: str | None = None) -> int:
        with self.lock:
            jobs = [j for j in {id(x): x for x in self.jobs.values()}.values() if j.contributed and not j.deleted
                    and (device_id is None or j.device_id == device_id) and (preview_id is None or j.preview_id == preview_id)]
        for j in jobs:
            self.delete(j)
        return len(jobs)

    def create(self, device_id: str, url: str, headers: dict[str, str], at_ms: int | None,
               playhead_ms: int | None, layout: str, alt: AltMeta | None = None, hints: dict | None = None,
               policy=None, contributed: bool = False) -> Job:
        pol = policy or self.policy
        try:
            pol.vet(url)                      # syntax + resolve + address check, before any I/O
        except Denied:
            raise SourceForbidden() from None
        url_key = hashlib.sha256(url.encode("utf-8", "surrogatepass")).hexdigest()
        now = self.clock()
        with self.lock:
            ex = self.url_index.get((device_id, url_key))
            if ex is not None:
                ex = ex.resolve()
                if not ex.deleted and ex.error is None:
                    ex.last_activity = now
                    if hints and ex.fp and self.library is not None:
                        self.library.record(ex.fp, hints, self.size_of(ex.fp), replace=not ex.contributed)
                    if alt is not None and self.settings.alt_source:
                        if ex.alt is None:
                            ex.alt = alt
                        elif ex.alt.state not in ("validating", "active"):
                            # the client re-sent its list (new app build, refreshed sources, job renewal): the newest
                            # metadata wins until a validation has started; attempts already made are kept
                            ex.alt.play, ex.alt.cands = alt.play, alt.cands
                    self._apply_priority(ex, at_ms, playhead_ms)
                    src = self.sources.get(ex.fp) if ex.fp else None
                    if src is not None:
                        src.touch()
                        self._ensure_running(src, device_id)
                    return ex
            freeable = self._device_freeable(device_id)
            excess = max(0, len(freeable) - (self.settings.max_jobs_per_device - 1))
            if contributed:
                running = self.running_sources()
                if (sum(1 for s in running if s.contributed) >= self.settings.contrib_max_sources
                        or len(running) + self.reserved >= self.settings.max_active_sources):
                    raise Busy()
            elif len(self.running_sources()) + self.reserved - excess >= self.settings.max_active_sources:
                # the OWNER always wins a slot: stop guest-contributed work instead of refusing the owner
                gone = [s for s in self.running_sources() if s.contributed]
                for s in gone:
                    threading.Thread(target=s.stop, args=("owner_priority",), daemon=True).start()
                excess += len(gone)
            if not contributed and len(self.running_sources()) + self.reserved - excess >= self.settings.max_active_sources:
                raise Busy()
            self.reserved += 1
            job = Job(new_preview_id(), device_id, layout, url_key, now, now,
                      pending_priority=(at_ms, playhead_ms),
                      alt=alt if self.settings.alt_source else None, hints=hints,
                      contributed=contributed, policy=pol if contributed else None)
            self.jobs[job.preview_id] = job
            self.url_index[(device_id, url_key)] = job
            self._trim_jobs()
        fut = self.pool.submit(self._preflight_and_attach, job, url, headers)
        try:
            fut.result(timeout=self.settings.create_wait_s)
        except concurrent.futures.TimeoutError:
            pass
        with self.lock:
            job.announced = True
            return job.resolve()

    def _capture_source(self, up: Upstream, size: int) -> None:
        """Diagnostics only (default off): keep the resolved CDN link in one owner-only file; never logged."""
        p = Path(self.settings.capture_source_file)
        fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(up.final.url())
        p.with_name(p.name + ".info.json").write_text(json.dumps({"size": size}))

    def _preflight_and_attach(self, job: Job, url: str, headers: dict[str, str]) -> None:
        job.diagnostic_host = urlsplit(url).hostname
        up = Upstream(job.policy or self.policy, url, headers,
                      Budget(self.settings.job_bytes, request_bucket=self.request_bucket),
                      self.settings.upstream_timeout_s)
        err = None
        pre = None
        try:
            pre = up.preflight()
            if pre.fmt is None:
                err = "not_video"
            elif pre.fmt == "matroska" and not pre.has_index:
                err = "no_index"
            if err is None and self.settings.capture_source_file and up.final is not None:
                self._capture_source(up, pre.size)
        except UpstreamError as e:
            err = e.code if e.code != "internal" else "internal"
            job.diagnostic_host = getattr(up, "last_tls_host", job.diagnostic_host)
            # Reasons are internal fixed codes; never print arbitrary exception text.
            safe_reason = e.reason if e.reason.replace("_", "").isalnum() else "other"
            log.warning("preflight failed code=%s reason=%s", err, safe_reason)
        except Exception:  # noqa: BLE001
            log.exception("preflight crashed")
            err = "internal"
        with self.lock:
            self.reserved -= 1
            if err is not None or job.deleted:
                up.close()
                job.error = err if not job.deleted else job.error
                job.done_event.set()
                self.bump()
                return
            fp = pre.fp
            target = self._canonical(job, fp)
            target.fp = fp
            if job.hints and self.library is not None:        # no-op unless the owner opted in
                self.library.record(fp, job.hints, pre.size, replace=not job.contributed)
            if job.alt is not None and target.alt is None:
                target.alt = job.alt
            src = self.sources.get(fp)
            if src is None:
                self.cache.ensure(fp)
                src = Source(self, fp)
                if job.contributed:
                    src.contributed = True                 # same workers as the owner when idle; fewer while the owner is busy
                self.sources[fp] = src
            elif not job.contributed:
                src.contributed = False                    # the owner opened the same title: it is the owner's source now
            src.jobs.add(target.preview_id)
            if not src.offer_upstream(up, pre):
                up.close()
            at_ms, playhead_ms = job.pending_priority
            self._apply_priority(target, at_ms, playhead_ms)
            src.touch()
            self._enforce_device_limit(job.device_id, keep=src)
            self._ensure_running(src, job.device_id, reserved_slot=True)
            job.done_event.set()
            self.bump()

    def _canonical(self, job: Job, fp: str) -> Job:
        """Dedup per (device, fingerprint): returns the job that owns the preview from now on."""
        for other in list(self.jobs.values()):
            if other is not job and not other.deleted and other.device_id == job.device_id \
                    and other.fp == fp and other.merged_into is None and other.error is None:
                if not job.announced:
                    job.merged_into = other
                    self.jobs.pop(job.preview_id, None)
                else:
                    self.jobs[job.preview_id] = other          # alias id -> same job
                    self.db.add_preview(job.preview_id, job.device_id, fp)
                self.url_index[(job.device_id, job.url_key)] = other
                other.last_activity = self.clock()
                return other
        db_id = self.db.preview_for(job.device_id, fp)
        if db_id is not None and db_id != job.preview_id and not job.announced:
            self.jobs.pop(job.preview_id, None)
            job.preview_id = db_id                              # same device + content -> same id
            self.jobs[db_id] = job
        else:
            self.db.add_preview(job.preview_id, job.device_id, fp)
        return job

    def _apply_priority(self, job: Job, at_ms: int | None, playhead_ms: int | None) -> None:
        src = self.sources.get(job.fp) if job.fp else None
        if src is not None:
            src.set_priority(at_ms, playhead_ms)

    def _enforce_device_limit(self, device_id: str, keep: Source) -> None:
        mine = [s for s in self._device_freeable(device_id) if s is not keep]
        allowed = self.settings.max_jobs_per_device - 1        # `keep` takes one slot
        while len(mine) > max(0, allowed):
            s = mine.pop(0)
            threading.Thread(target=s.stop, args=("superseded",), daemon=True).start()

    def _ensure_running(self, src: Source, device_id: str, reserved_slot: bool = False) -> None:
        if src.running or src.error is not None:
            return
        running = [s for s in self.running_sources() if not s.stop_flag]
        if not reserved_slot and len(running) + self.reserved >= self.settings.max_active_sources:
            return
        src.start()

    # ---------------------------------------------------------------- lookups
    def get(self, device_id: str, preview_id: str) -> Job | None:
        with self.lock:
            j = self.jobs.get(preview_id)
            if j is None or j.device_id != device_id:
                return None
            j = j.resolve()
            return None if j.deleted else j

    def lookup_data(self, preview_id: str) -> tuple[str, str | None, Job | None] | None:
        with self.lock:
            j = self.jobs.get(preview_id)
            if j is not None:
                j = j.resolve()
                return j.device_id, j.fp, j
        row = self.db.preview(preview_id)
        if row is None:
            return None
        return row[0], row[1], None

    def touch(self, job: Job) -> None:
        job.last_activity = self.clock()
        src = self.sources.get(job.fp) if job.fp else None
        if src is not None:
            src.touch()
        if job.fp:
            self.cache.touch(job.fp)

    def priority(self, job: Job, at_ms: int | None, playhead_ms: int | None) -> None:
        with self.lock:
            self.touch(job)
            if job.fp is None:
                job.pending_priority = (at_ms, playhead_ms)
                return
            src = self.sources.get(job.fp)
            if src is None:
                return
            src.set_priority(at_ms, playhead_ms)
            self._ensure_running(src, job.device_id)

    def delete(self, job: Job) -> None:
        with self.lock:
            job.deleted = True
            for pid, j in list(self.jobs.items()):
                if j is job:
                    del self.jobs[pid]
            for key, j in list(self.url_index.items()):
                if j is job:
                    del self.url_index[key]
            src = self.sources.get(job.fp) if job.fp else None
            if src is not None:
                src.jobs.discard(job.preview_id)
                remaining = [p for p in src.jobs if p in self.jobs]
            else:
                remaining = []
        if src is not None and not remaining:
            src.stop("cancel")
        self.bump()

    def revoke_device(self, device_id: str) -> None:
        with self.lock:
            mine = {id(j): j for j in self.jobs.values() if j.device_id == device_id}
        for j in mine.values():
            self.delete(j)

    # ---------------------------------------------------------------- status
    def version(self, job: Job) -> int:
        if job.fp is None:
            return 0
        e = self.cache.entry(job.fp)
        return e.version if e else 0

    def state(self, job: Job) -> tuple[str, str | None]:
        if job.error:
            return "failed", job.error
        if job.fp is None:
            return "preparing", None
        src = self.sources.get(job.fp)
        if src is not None and src.error:
            return "failed", src.error
        e = self.cache.entry(job.fp)
        tiles = len(e.tiles) if e else 0
        n = e.n_tiles if e else None
        sch = src.scheduler if src else None
        if src is not None and src.quality != self.quality:
            sch = None                                  # its scheduler still counts the previous quality
        if n and (tiles >= n or (sch is not None and sch.complete_all)):
            return "complete", None
        if tiles or (e is not None and self.cache.cached_qualities(job.fp)):
            return "ready", None                        # (the VTT shows other qualities' tiles meanwhile)
        return "preparing", None

    def snapshot(self, job: Job) -> dict:
        state, err = self.state(job)
        e = self.cache.entry(job.fp) if job.fp else None
        return {"state": state, "error": err, "vtt_version": self.version(job),
                "source_duration_ms": e.duration_ms if e else None,
                "tiles_done": len(e.tiles) if e else 0, "tiles_total": e.n_tiles if e else None}

    # ---------------------------------------------------------------- alternative source
    def alt_gain(self, src: Source, meta: AltMeta) -> tuple[bool | None, dict]:
        """Break-even: is switching to a lower-resolution source worth its validation cost?
        Returns (True | False | None=not known yet, info).

        Projected time for the playback source to finish = remaining tiles / its measured throughput. Time via the
        alternative = expected validation + the remaining tiles at an assumed speed-up (bytes per tile scale with
        bitrate, capped at 3x; 1x if unknown). Negotiate only when the saving reaches `alt_min_gain_s`; otherwise no
        link is resolved and no validation traffic is spent. Until the playback source has produced a few tiles its
        rate is unknown, so the decision waits (at most ALT_RATE_WAIT_S), then assumes it is slow."""
        s = self.settings
        n = self.scheduler_n(src)
        info = {"remaining": None, "validation_est_s": None, "a_rate": None, "projected_a_s": None, "gain_s": None}
        if n == 0:
            return None, info                                   # film length not known yet
        e = self.cache.entry(src.fp)
        remaining = max(0, n - (len(e.tiles) if e else 0))
        info["remaining"] = remaining
        if remaining == 0:
            return False, info
        now = self.clock()
        val_s = self.alt_validation_s if self.alt_validation_s is not None else s.alt_validation_est_s
        info["validation_est_s"] = round(val_s, 1)
        ev = sorted((x for x in src.events if not x.via_alt and now - x.at <= 10.0), key=lambda x: x.at)
        if len(ev) < 6:                                        # too few tiles for a steady rate (startup ramp-up)
            return (None if now - src.created < ALT_RATE_WAIT_S else True), info
        rate = (len(ev) - 1) / max(0.2, ev[-1].at - ev[0].at)  # inter-arrival rate: excludes the idle startup
        projected = remaining / rate
        # What binds the alternative source? Estimate its tile rate from the playback source's measured cost per
        # tile (bytes scale with bitrate but have a floor from the read-window size) and from the alt budgets:
        # request-bound sources cannot go faster than alt_rps, byte-bound ones than alt_mbps / bytes-per-tile.
        reqs = sum(x.requests for x in ev) / len(ev)
        byts = sum(x.bytes for x in ev) / len(ev)
        ratio_b = 0.6
        if meta.cands and meta.play.bitrate_bps > 0:
            ratio_b = min(1.0, max(0.4, min(c.bitrate_bps or 1e18 for c in meta.cands) / meta.play.bitrate_bps))
        rate_b = float("inf")
        if reqs > 0:
            rate_b = min(rate_b, s.alt_rps / reqs)
        if byts > 0:
            rate_b = min(rate_b, s.alt_mbps * 125_000 / (byts * ratio_b))
        speed = 1.0 if rate_b == float("inf") else max(1.0, rate_b / rate)
        info["speedup"] = round(speed, 2)
        via_alt = (val_s + max(0.0, remaining - rate * val_s) / (rate * speed)) if projected > val_s else projected
        gain = projected - via_alt
        info.update(a_rate=round(rate, 2), projected_a_s=round(projected, 1), gain_s=round(gain, 1))
        return gain >= s.alt_min_gain_s, info

    def alt_view(self, job: Job) -> dict | None:
        """Status block for the preview response (absent unless the feature is on and the client opted in).

        The break-even decision is re-evaluated while the playback source is still running: the first tiles near the
        playhead are not byte-throttled, so an early rate can look faster than the steady one. A skipped candidate
        can therefore become wanted later, never the other way round once it has been offered."""
        m = job.resolve().alt
        if m is None or not self.settings.alt_source:
            return None
        j = job.resolve()
        src = self.sources.get(j.fp) if j.fp else None
        ready = src is not None and src.upstream is not None           # nothing to compare against before that
        want = None
        with self.lock:
            if m.hold and m.state in ("wanted", "none", "rejected"):
                return {"state": "held", "want": None, "reason": m.reason or None}
            if self.settings.alt_dry_run:                              # negotiate and report only: never ask for a link
                m.state, m.reason = "dry_run", "dry_run"
                return {"state": m.state, "want": None, "reason": m.reason}
            if ready and m.state in ("wanted", "none") and m.next_wanted_any() is not None:
                if self.clock() < m.cooldown_until:                    # a provider asked us to back off
                    return {"state": m.state, "want": None, "reason": m.reason or None}
                worthwhile, info = self.alt_gain(src, m)
                m.gain = info
                if worthwhile:
                    if m.state == "none":                              # was skipped, now worth it
                        m.state = "wanted"
                        if m.reason == "not_worthwhile":
                            m.reason = ""                              # a rejection reason stays until the next attempt
                    want = m.next_wanted()
                    if want is not None and not src.governor.healthy():
                        want = None          # only ASK when the answer would be accepted: no fresh, healthy telemetry
                                             # for this job yet means the client would be refused (and give up)
                elif worthwhile is False:
                    m.state, m.reason = "none", "not_worthwhile"
            return {"state": m.state, "want": want, "reason": m.reason or None}

    def telemetry(self, job: Job, buffer_ms: int | None, rebuffering: bool, host: str | None, conns: int) -> None:
        """Playback health for THIS job's source only (never shared across devices or jobs)."""
        if not (self.settings.alt_source or self.settings.playback_guard):
            return
        j = job.resolve()
        src = self.sources.get(j.fp) if j.fp else None
        if src is not None:
            seeking = (self.clock() - src.last_seek_at) < 15.0     # buffering right after a seek is not a stall
            src.governor.playback(buffer_ms, rebuffering, host, conns, seeking)

    def alt_attach(self, job: Job, cid: str, url: str, headers: dict[str, str]) -> bool:
        """Client supplied the resolved link for candidate `cid`. False = not wanted / not allowed / not ready."""
        job = job.resolve()
        m = job.alt
        if m is None or not self.settings.alt_source:
            return False
        src = self.sources.get(job.fp) if job.fp else None
        if src is None or src.upstream is None:
            return False
        try:
            self.policy.vet(url)
        except Denied:
            with self.lock:
                m.tried.add(cid)
                m.reason = "source_forbidden"
            return False
        with self.lock:                                                # compare-and-set: one validation per offer
            if self.settings.alt_dry_run or m.hold or m.state != "wanted" or m.next_wanted() != cid \
                    or self.clock() < m.cooldown_until:
                return False
            now = self.clock()
            while self.alt_resolve_times and now - self.alt_resolve_times[0] > 300.0:
                self.alt_resolve_times.popleft()
            if len(self.alt_resolve_times) >= self.settings.alt_max_resolutions_5min:
                m.reason = "resolution_budget"                          # provider API budget: do not resolve more links
                return False
            if not src.governor.healthy():                             # never add probe traffic while playback is at risk
                return False
            m.state = "validating"
        self.alt_pool.submit(self._alt_task, job, src, cid, url, headers)
        return True

    def _alt_task(self, job: Job, src: Source, cid: str, url: str, headers: dict[str, str]) -> None:
        m = job.alt
        up = cap = psrc = None
        t0 = time.perf_counter()
        rec: dict = {"cid": cid, "outcome": None, "reason": None, "range_ok": False}
        if self.settings.alt_report_names or self.settings.alt_dry_run:
            rec["name"] = next((c.name[:120] for c in m.cands if c.cid == cid), None)
        try:
            up = Upstream(self.policy, url, headers,
                          Budget(self.settings.job_bytes, request_bucket=self.alt_request_bucket,
                                 urgent=lambda: src.urgent), self.settings.upstream_timeout_s)
            pre = up.preflight()
            rec.update(range_ok=True, size_mb=round(pre.size / 1e6), fmt=pre.fmt)      # link resolved, Range honoured
            if pre.fp == src.fp:                                       # the same file under another link: nothing to gain
                raise UpstreamError("internal", "alt_same_file")
            if pre.fmt is None or (pre.fmt == "matroska" and not pre.has_index):
                raise UpstreamError("not_video", "alt_format")
            psrc = ProxySource(up, pre.size, [(0, pre.head), (pre.tail_offset, pre.tail)],
                               background=lambda: src.background, bucket=self.alt_bucket,
                               shared_cache=pre.size >= 16 * 1024 * 1024)
            cap = self.proxy.register(psrc)
            e = self.cache.entry(src.fp)
            deadline = time.monotonic() + 20
            while (e is None or not e.duration_ms) and time.monotonic() < deadline and not src.stop_flag:
                time.sleep(0.1)
                e = self.cache.entry(src.fp)
            if src.stop_flag or e is None or not e.duration_ms:
                raise UpstreamError("internal", "alt_no_duration")
            worthwhile, ginfo = self.alt_gain(src, m)
            if worthwhile is False and ginfo.get("a_rate") is not None:  # the playback source got ahead meanwhile
                raise UpstreamError("internal", "alt_not_worthwhile")
            v = validate(self, src, cap, pre.size, pre.fmt, e.duration_ms)
            log.info("alt candidate validated=%s reason=%s inliers=%d grabs a=%d b=%d in %.1fs",
                     v.accepted, v.reason, v.anchors, v.samples_a, v.samples_b, time.perf_counter() - t0)
            rec.update(validated=bool(v.accepted), verdict=v.reason, detail=v.detail, anchors=v.anchors,
                       grabs_a=v.samples_a, grabs_b=v.samples_b, validation_seconds=round(time.perf_counter() - t0, 2))
            if not v.accepted or src.stop_flag:
                raise UpstreamError("source_unreachable", "alt_" + v.reason)
            mp = v.mapping
            tol_b = mp.tolerance_for_b(self.settings.tolerance_ms)     # the budget is the CONFIGURED tolerance
            if tol_b < 500:
                raise UpstreamError("source_unreachable", "alt_mapping_uncertain_for_tolerance")
            host = urlsplit(url).hostname or "?"
            alt = AltState(cid, mp, pre.fp, up, psrc, cap, host, pre.fmt, pre.size, tol_b, since=self.clock())
            with src.cond:
                if src.stop_flag:                                      # stopped while validating: do not resurrect
                    raise UpstreamError("internal", "alt_stopped")
                self.db.put_thumb_source(src.fp, pre.fp, mp.a, mp.b, mp.fit_err_ms, mp.kind)
                src.alt = alt
                src.cond.notify_all()
            took = time.perf_counter() - t0
            self.alt_validation_s = took if self.alt_validation_s is None else 0.5 * self.alt_validation_s + 0.5 * took
            m.state, m.reason = "active", ""
            rec["outcome"] = "accepted"
            m.validation = {"seconds": round(took, 2), "a": v.samples_a, "b": v.samples_b, "kind": mp.kind,
                            "fit_err_ms": round(mp.fit_err_ms), "tol_b_ms": tol_b, "detail": v.detail}
        except Exception as ex:  # noqa: BLE001 - every failure leaves the playback source in charge
            reason = getattr(ex, "reason", None) or type(ex).__name__
            log.info("alt candidate rejected: %s", str(reason)[:60])
            err = getattr(psrc, "last_error", None) or (ex if isinstance(ex, UpstreamError) else None)
            ra = getattr(err, "retry_after", None)
            rec.update(outcome="rejected", reason=str(reason)[:60])
            with self.lock:
                m.tried.add(cid)
                m.state = "wanted" if len(m.tried) < 2 else "rejected"
                m.reason = str(reason).replace("alt_", "")[:40]
                if self.settings.alt_hold_after_reject and ra is None:
                    m.hold = True                                              # a person decides before the next link is asked for
                if ra is not None:                                                 # provider rate limit: back off before offering another
                    m.cooldown_until = self.clock() + min(max(float(ra), 5.0), 600.0)   # never less than 5 s after a 429
                    src.governor.retry_after(urlsplit(url).hostname or "?", ra)
            if cap:
                self.proxy.unregister(cap)
            if up is not None:
                up.close()
        finally:
            if up is not None:
                snap = up.stats.snapshot()
                m.attempts.append({**rec, **snap})
                with self.lock:
                    self.alt_resolve_times.extend([self.clock()] * max(1, snap["resolutions"]))
            self.bump()

    def alt_deactivate(self, src: Source, reason: str, final: bool = True) -> None:
        """Drop the alternative source: unregister its proxy cap, close its upstream. `final=False` (the source was
        merely stopped) lets the client negotiate again on resume."""
        with src.cond:
            alt, src.alt = src.alt, None
        if alt is None:
            return
        alt.active = False
        for j in list(self.jobs.values()):
            if j.fp == src.fp and j.alt is not None:
                if final:
                    j.alt.state, j.alt.reason = "failed", reason[:40]
                else:
                    j.alt.state, j.alt.reason, j.alt.tried = "wanted", "", set()
        self.proxy.unregister(alt.cap)
        alt.upstream.close()

    def alt_spot_check(self, src: Source) -> None:
        """Closed loop after the grid completes: tiles made from the alternative source must still look like A.
        Only the alternative-made tiles are dropped, in one transaction, and the runner is woken once."""
        alt = src.alt
        n = self.scheduler_n(src)
        if alt is None or n == 0:
            return
        made = sorted(k for k in src.alt_slots if 0 <= k < n)
        if not made:
            return
        picks = sorted({made[int(len(made) * (i + 0.5) / 6)] for i in range(6)})
        try:
            checked, failed = spot_check(self, src, picks)
        except Exception:  # noqa: BLE001
            return
        for m in [j.alt for j in self.jobs.values() if j.fp == src.fp and j.alt is not None]:
            m.spot = {"checked": checked, "failed": failed}
        if checked >= SPOT_MIN_CHECKED and failed > SPOT_MAX_FAILS and not src.stop_flag:
            log.warning("alt spot check failed (%d of %d); dropping %d alternative tiles", failed, checked, len(made))
            self.alt_deactivate(src, "spot_check")
            dropped = self.cache.drop_tiles_quiet(src.fp, made)
            with src.cond:
                for k in made:
                    if src.scheduler is not None:
                        src.scheduler.mark_missing(k)
                src.alt_slots.difference_update(made)
                src.cond.notify_all()
            if dropped:
                self._tile_lost(src.fp, made[0])                       # one wake-up restarts the finished runner

    def alt_release(self) -> int:
        """Admin: let held negotiations offer their next candidate. Returns how many were released."""
        n = 0
        with self.lock:
            for j in self.jobs.values():
                if j.alt is not None and j.alt.hold:
                    j.alt.hold = False
                    n += 1
        return n

    def alt_metrics(self, src: Source) -> dict | None:
        """Diagnostics for /admin/metrics: no URLs, no names, no headers."""
        metas = [j.alt for j in self.jobs.values() if j.fp == src.fp and j.alt is not None]
        if not metas or not self.settings.alt_source:
            return None
        m = metas[0]
        alt = src.alt
        now = self.clock()
        recent = [t for t in self.alt_resolve_times if now - t <= 300.0]
        return {"state": m.state, "reason": m.reason or None, "dry_run": self.settings.alt_dry_run,
                "candidates": explain(m.play, m.cands, with_names=self.settings.alt_dry_run or self.settings.alt_report_names),
                "tried": sorted(m.tried), "hold": m.hold,
                "gain": m.gain, "validation": m.validation, "spot": m.spot,
                "attempts": m.attempts, "active": alt is not None and alt.active,
                "upstream": alt.upstream.stats.snapshot() if alt is not None else None,
                "tol_b_ms": alt.tol_b if alt is not None else None,
                "alt_tiles": len(src.alt_slots), "governor": src.governor.stats(),
                "resolutions_5min": len(recent), "resolution_limit_5min": self.settings.alt_max_resolutions_5min}

    def scheduler_n(self, src: Source) -> int:
        return src.scheduler.n if src.scheduler is not None else 0

    # ---------------------------------------------------------------- housekeeping
    def _trim_jobs(self) -> None:
        canon = [j for pid, j in self.jobs.items() if j.preview_id == pid]
        if len(canon) <= self.settings.max_jobs_total:
            return
        canon.sort(key=lambda j: j.last_activity)
        for j in canon[: len(canon) - self.settings.max_jobs_total]:
            src = self.sources.get(j.fp) if j.fp else None
            if src is not None and src.running:
                continue
            for pid, x in list(self.jobs.items()):
                if x is j:
                    del self.jobs[pid]
            for key, x in list(self.url_index.items()):
                if x is j:
                    del self.url_index[key]

    def _reap_loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self.reap()
            except Exception:  # noqa: BLE001
                log.exception("reaper error")

    def reap(self) -> None:
        now = self.clock()
        idle = []
        with self.lock:
            for s in self.sources.values():
                if s.running and now - s.last_activity > self.settings.idle_timeout_s:
                    idle.append(s)
        for s in idle:
            s.stop("idle")
        if now - self._last_evict > 60:
            self._last_evict = now
            with self.lock:
                protected = {fp for fp, s in self.sources.items() if s.running}
            for fp in self.cache.evict(protected):
                with self.lock:
                    s = self.sources.pop(fp, None)
                if s is not None:
                    s.stop("evicted")

    def shutdown(self) -> None:
        self._stop.set()
        with self.lock:
            srcs = list(self.sources.values())
        for s in srcs:
            s.stop("shutdown")
            if s.proxy_cap:
                self.proxy.unregister(s.proxy_cap)
            if s.upstream is not None:
                s.upstream.close()
            if s.alt is not None:
                self.alt_deactivate(s, "shutdown")
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.alt_pool.shutdown(wait=False, cancel_futures=True)
