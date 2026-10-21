"""Extraction worker: ONE subprocess per active source.

Child  (`python -m openseek.worker`, or the bootstrap below): reads JSON-line requests on
       stdin, answers on stdout. It holds one persistent `engine.Extractor` whose reader can
       only reach the loopback range proxy. It has no access to the source URL or headers.
Parent (`WorkerEngine`): starts the child (argument vector, never a shell), enforces the
       open/per-frame timeouts by killing the process, and reports crashes so the runner can
       restart it with backoff.

Protocol (one JSON object per line):
  -> {"op":"open","url":"http://127.0.0.1:P/j/<cap>","size":N,"format":"matroska","max_dim":8192,
      "max_duration_s":43200}
  <- {"ok":true,"duration_ms":..,"width":..,"height":..,"codec":"h264","hdr":false,"container":".."}
  -> {"op":"grab","t_ms":4800000,"accurate":false,"thumb":[320,180,75]}
     ("thumb" = [width, height, JPEG quality] of a quality preset; absent: Standard. Also on grab_batch.)
  <- {"ok":true,"pts_ms":4797960,"jpeg":"<base64>","requests":2,"bytes":812345}
  <- {"ok":false,"error":"not_video|unsupported_codec|upstream|internal","detail":".."}
"""
from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)
# -I: isolated mode (ignores PYTHON* env vars, user site and the CWD); the package dir is
# passed explicitly instead.
_BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from openseek.worker import main; main()"


# ===================================================================== child
def main() -> None:  # pragma: no cover - exercised through subprocess in integration tests
    out = sys.stdout
    sys.stdout = sys.stderr          # nothing but protocol lines may reach the real stdout
    if hasattr(os, "nice"):
        try:
            os.nice(5)
        except OSError:
            pass
    try:
        import av
        av.logging.set_level(av.logging.PANIC)
    except Exception:  # noqa: BLE001
        pass
    from .engine import EngineError, Extractor, ProxyReader
    from .quality import from_wire

    ex = None

    def thumb(msg: dict) -> None:
        if "thumb" in msg:
            q = from_wire(msg["thumb"])
            if q is None:
                raise EngineError("internal", "bad_thumb")      # only the presets' exact geometries are rendered
            ex.set_thumb(q.w, q.h, q.jpeg_q)

    def reply(obj: dict) -> None:
        out.write(json.dumps(obj, separators=(",", ":")) + "\n")
        out.flush()

    for line in sys.stdin:
        try:
            msg = json.loads(line)
            op = msg.get("op")
            if op == "open":
                if ex is not None:
                    ex.close()
                rd = ProxyReader(msg["url"], int(msg["size"]))
                ex = Extractor(rd, msg["format"], int(msg.get("max_dim", 8192)),
                               float(msg.get("max_duration_s", 43200)),
                               nearest_kf=bool(msg.get("nearest_kf", False)))
                i = ex.info()
                reply({"ok": True, "duration_ms": i.duration_ms, "width": i.width, "height": i.height,
                       "codec": i.codec, "hdr": i.hdr, "container": i.container})
            elif op == "grab":
                if ex is None:
                    raise EngineError("internal", "not_open")
                thumb(msg)
                q0, b0 = ex.reader.requests, ex.reader.bytes_fetched
                tol_ms = int(msg.get("tolerance_ms", 5000))
                pts, jpeg = ex.grab(int(msg["t_ms"]), bool(msg.get("accurate")), tolerance_ms=tol_ms)
                reply({"ok": True, "pts_ms": pts, "jpeg": base64.b64encode(jpeg).decode("ascii"),
                       "requests": ex.reader.requests - q0, "bytes": ex.reader.bytes_fetched - b0})
            elif op == "grab_batch":
                if ex is None:
                    raise EngineError("internal", "not_open")
                thumb(msg)
                q0, b0 = ex.reader.requests, ex.reader.bytes_fetched
                tol_ms = int(msg.get("tolerance_ms", 5000))
                items = ex.grab_batch([int(x) for x in msg["targets_ms"]], tolerance_ms=tol_ms)
                reply({"ok": True,
                       "items": [{"t_ms": t, "pts_ms": pts, "accurate": acc,
                                  "jpeg": base64.b64encode(jpg).decode("ascii")}
                                 for t, pts, acc, jpg in items],
                       "requests": ex.reader.requests - q0, "bytes": ex.reader.bytes_fetched - b0})
            elif op == "keyframes":
                if ex is None:
                    raise EngineError("internal", "not_open")
                reply({"ok": True, "kf": ex.keyframe_times()})
            elif op == "kfindex":
                if ex is None:
                    raise EngineError("internal", "not_open")
                reply({"ok": True, "kf": [[t, p] for t, p in ex.keyframe_index()]})
            elif op == "quit":
                break
            else:
                reply({"ok": False, "error": "internal", "detail": "unknown_op"})
        except EngineError as e:
            reply({"ok": False, "error": e.code, "detail": e.detail})
        except Exception as e:  # noqa: BLE001 - report the type only (no paths/URLs)
            reply({"ok": False, "error": "internal", "detail": type(e).__name__})
    if ex is not None:
        ex.close()


# ===================================================================== parent
class WorkerError(Exception):
    """code: contract-ish engine code; transient: True for crash/timeout (restart and retry)."""

    def __init__(self, code: str, detail: str = "", transient: bool = False):
        super().__init__(f"{code}:{detail}")
        self.code = code
        self.detail = detail
        self.transient = transient


@dataclass
class OpenResult:
    duration_ms: int
    width: int
    height: int
    codec: str
    hdr: bool


@dataclass
class GrabResult:
    pts_ms: int
    jpeg: bytes
    requests: int = 0
    bytes: int = 0
    via: str = "a"          # "b" when produced from an alternative thumbnail source


def worker_argv() -> list[str]:
    """The worker's argument vector. Frozen (PyInstaller): the same executable with a marker
    argument (see run_server.py); otherwise an isolated interpreter running the bootstrap."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--openseek-worker"]
    return [sys.executable, "-I", "-c", _BOOT, PACKAGE_PARENT]


def _child_env() -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
    return {k: os.environ[k] for k in keep if k in os.environ}


class WorkerProcess:
    def __init__(self):
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0) | getattr(
                subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            worker_argv(),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=_child_env(), creationflags=flags, close_fds=True)
        self._lines: queue.Queue = queue.Queue()
        self._reader = threading.Thread(target=self._pump, name="openseek-worker-out", daemon=True)
        self._reader.start()
        self._lock = threading.Lock()

    @property
    def pid(self) -> int:
        return self.proc.pid

    def _pump(self) -> None:
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        self._lines.put(None)
        # the pipes are closed here, by the only reader, never from another thread (closing a
        # pipe that a thread is blocked on deadlocks on Windows)
        for f in (self.proc.stdout, self.proc.stdin):
            try:
                f.close()
            except (OSError, ValueError):
                pass

    def alive(self) -> bool:
        return self.proc.poll() is None

    def call(self, msg: dict, timeout: float) -> dict:
        with self._lock:
            if not self.alive():
                raise WorkerError("crash", "dead", transient=True)
            try:
                self.proc.stdin.write((json.dumps(msg) + "\n").encode())
                self.proc.stdin.flush()
            except (OSError, ValueError):
                self.kill()
                raise WorkerError("crash", "stdin", transient=True) from None
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                self.kill()                          # hard per-frame timeout
                raise WorkerError("timeout", "", transient=True) from None
            if line is None:
                self.kill()
                raise WorkerError("crash", "eof", transient=True)
            try:
                return json.loads(line)
            except ValueError:
                self.kill()
                raise WorkerError("crash", "protocol", transient=True) from None

    def kill(self) -> None:
        if self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def quit(self) -> None:
        if self.alive():
            try:
                self.proc.stdin.write(b'{"op":"quit"}\n')
                self.proc.stdin.flush()
                self.proc.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self.kill()


class WorkerEngine:
    """Engine used by the source runner: a (re)startable worker subprocess."""

    def __init__(self, proxy_url: str, size: int, fmt: str, open_timeout_s: float, frame_timeout_s: float,
                 max_dim: int, max_duration_s: float, tolerance_ms: int = 5000,
                 nearest_kf: bool = False):
        self.proxy_url = proxy_url
        self.size = size
        self.fmt = fmt
        self.open_timeout_s = open_timeout_s
        self.frame_timeout_s = frame_timeout_s
        self.max_dim = max_dim
        self.max_duration_s = max_duration_s
        self.tolerance_ms = int(tolerance_ms)
        self.nearest_kf = bool(nearest_kf)
        self.thumb: list[int] | None = None       # quality preset geometry for grabs (None: the worker's default, Standard)
        self.proc: WorkerProcess | None = None
        self.started_at = 0.0

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc and self.proc.alive() else None

    def is_open(self) -> bool:
        return self.proc is not None and self.proc.alive()

    def open(self) -> OpenResult:
        self.close()
        self.proc = WorkerProcess()
        self.started_at = time.monotonic()
        r = self.proc.call({"op": "open", "url": self.proxy_url, "size": self.size, "format": self.fmt,
                            "max_dim": self.max_dim, "max_duration_s": self.max_duration_s,
                            "nearest_kf": self.nearest_kf},
                           self.open_timeout_s)
        if not r.get("ok"):
            self.close()
            raise WorkerError(r.get("error", "internal"), r.get("detail", ""))
        return OpenResult(int(r["duration_ms"]), int(r["width"]), int(r["height"]), str(r["codec"]),
                          bool(r["hdr"]))

    def grab(self, t_ms: int, accurate: bool, tolerance_ms: int | None = None) -> GrabResult:
        if not self.is_open():
            raise WorkerError("crash", "not_open", transient=True)
        tol = self.tolerance_ms if tolerance_ms is None else int(tolerance_ms)
        timeout = self.frame_timeout_s * (2 if accurate else 1)
        msg = {"op": "grab", "t_ms": int(t_ms), "accurate": bool(accurate), "tolerance_ms": tol}
        if self.thumb is not None:
            msg["thumb"] = list(self.thumb)
        r = self.proc.call(msg, timeout)
        if not r.get("ok"):
            raise WorkerError(r.get("error", "internal"), r.get("detail", ""))
        return GrabResult(int(r["pts_ms"]), base64.b64decode(r["jpeg"]), int(r.get("requests", 0)),
                          int(r.get("bytes", 0)))

    def keyframes(self) -> list[int]:
        if not self.is_open():
            raise WorkerError("crash", "not_open", transient=True)
        r = self.proc.call({"op": "keyframes"}, self.frame_timeout_s)
        if not r.get("ok"):
            raise WorkerError(r.get("error", "internal"), r.get("detail", ""))
        return [int(x) for x in r.get("kf", [])]

    def keyframe_index(self) -> list[tuple[int, int]]:
        if not self.is_open():
            raise WorkerError("crash", "not_open", transient=True)
        r = self.proc.call({"op": "kfindex"}, self.frame_timeout_s)
        if not r.get("ok"):
            raise WorkerError(r.get("error", "internal"), r.get("detail", ""))
        return [(int(t), int(p)) for t, p in r.get("kf", [])]

    def grab_batch(self, targets_ms: list[int], tolerance_ms: int = 5000) -> list[tuple[int, bool, GrabResult]]:
        if not self.is_open():
            raise WorkerError("crash", "not_open", transient=True)
        timeout = self.frame_timeout_s * max(1, len(targets_ms))
        msg = {"op": "grab_batch", "targets_ms": [int(x) for x in targets_ms], "tolerance_ms": int(tolerance_ms)}
        if self.thumb is not None:
            msg["thumb"] = list(self.thumb)
        r = self.proc.call(msg, timeout)
        if not r.get("ok"):
            raise WorkerError(r.get("error", "internal"), r.get("detail", ""))
        out: list[tuple[int, bool, GrabResult]] = []
        reqs = int(r.get("requests", 0))
        byts = int(r.get("bytes", 0))
        for idx, it in enumerate(r.get("items", [])):
            gr = GrabResult(int(it["pts_ms"]), base64.b64decode(it["jpeg"]),
                            reqs if idx == 0 else 0, byts if idx == 0 else 0)
            out.append((int(it["t_ms"]), bool(it["accurate"]), gr))
        return out

    def set_thumb(self, wire: list[int] | None) -> None:
        self.thumb = list(wire) if wire is not None else None

    def kill(self) -> None:
        """Hard stop from another thread (cancel / idle / shutdown)."""
        p = self.proc
        if p is not None:
            p.kill()

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            p.quit()


if __name__ == "__main__":  # pragma: no cover
    main()
