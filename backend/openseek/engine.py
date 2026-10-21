"""Extraction engine (runs inside the worker subprocess only).

Productionised Gate 1 "S5": a persistent PyAV container over `ProxyReader`, a seekable file
object issuing bounded Range requests (256 KiB blocks, doubling read-ahead to 2 MiB on
sequential access, LRU block cache) to the backend's loopback range proxy - never to the
network. Decoder: slice threading, probesize 256 KiB, analyzeduration 0.5 s; keyframe seek
backwards and take the first decoded frame; accurate mode decodes forward to the target.
HDR (PQ/HLG) is tonemapped to SDR at thumbnail size (numpy Hable; PyAV wheels lack zscale).
Thumbnails: fit inside the preset's box (default Standard, 320x180) preserving display aspect,
letter/pillarbox to exactly that box, at the preset's JPEG quality (Standard: 75). See quality.py.
"""
from __future__ import annotations

import http.client
import io
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

from .quality import STANDARD

THUMB_W, THUMB_H = STANDARD.w, STANDARD.h
JPEG_QUALITY = STANDARD.jpeg_q
FORMAT_DEMUXER = {"matroska": "matroska", "mp4": "mov"}
FORMAT_ALLOW = ("matroska", "webm", "mov", "mp4")
TRC_PQ, TRC_HLG = 16, 18


class EngineError(Exception):
    """code: not_video | unsupported_codec | upstream | timeout | internal"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}:{detail}")
        self.code = code
        self.detail = detail


class ProxyReader(io.RawIOBase):
    def __init__(self, url: str, size: int, block: int = 256 * 1024, readahead_blocks: int = 8,
                 max_blocks: int = 128, timeout_s: float = 120.0):
        super().__init__()
        p = urlsplit(url)
        if p.scheme != "http" or p.hostname not in ("127.0.0.1", "::1") or not p.port:
            raise EngineError("internal", "reader_not_loopback")  # the worker never leaves loopback
        self.host, self.port, self.target = p.hostname, p.port, p.path
        self.size = int(size)
        self.block = block
        self.readahead = readahead_blocks
        self.max_blocks = max_blocks
        self.timeout = timeout_s
        self.cache: OrderedDict[int, bytes] = OrderedDict()
        self.pos = 0
        self.requests = 0
        self.bytes_fetched = 0
        self._last_block = -10
        self._run = 0
        self.seek_blocks = 1
        self.step_blocks = 2
        self.accurate_mode = False
        self.conn: http.client.HTTPConnection | None = None

    def configure_for_bitrate(self, bytes_per_sec: float) -> None:
        """Size post-seek reads to cover typical keyframe + cluster header in one Range request
        for high-bitrate remuxes, while staying at 1 block (256 KiB) for low-bitrate streams."""
        if bytes_per_sec >= 2_500_000:      # >= 20 Mbps (remuxes / 4K streams)
            self.seek_blocks = 4            # 1 MiB initial read on seek (+1 block if past half-block)
            self.step_blocks = 3            # 768 KiB follow-up step if keyframe spills past initial read
        elif bytes_per_sec >= 200_000:      # 1.6..20 Mbps (1080p episodes / web-dl)
            self.seek_blocks = 2            # 512 KiB initial read on seek (+1 block if past half-block)
            self.step_blocks = 2
        else:
            self.seek_blocks = 1
            self.step_blocks = 2

    def _request(self, start: int, end: int) -> bytes:
        for attempt in range(3):
            if self.conn is None:
                self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
            try:
                self.conn.request("GET", self.target, headers={"Range": f"bytes={start}-{end}"})
                r = self.conn.getresponse()
                self.requests += 1
                if r.status != 206:
                    r.read()
                    raise EngineError("upstream", r.getheader("X-OpenSeek-Error") or f"proxy_{r.status}")
                data = r.read()
                self.bytes_fetched += len(data)
                if len(data) != end - start + 1:
                    raise http.client.IncompleteRead(data)
                return data
            except (http.client.HTTPException, OSError):
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                if attempt == 2:
                    raise EngineError("upstream", "proxy_io") from None
        raise EngineError("upstream", "proxy_io")

    def _fetch(self, b0: int, nblocks: int) -> None:
        start = b0 * self.block
        end = min(self.size, (b0 + nblocks) * self.block) - 1
        if start > end:
            return
        data = self._request(start, end)
        for i in range(nblocks):
            chunk = data[i * self.block:(i + 1) * self.block]
            if not chunk:
                break
            self.cache[b0 + i] = chunk
            self.cache.move_to_end(b0 + i)
        while len(self.cache) > self.max_blocks:
            self.cache.popitem(last=False)

    def _get_block(self, b: int) -> bytes:
        if b not in self.cache:
            self._run = self._run + 1 if b == self._last_block + 1 else 0
            if self._run == 0:
                n = self.seek_blocks
                if n > 1 and (self.pos - b * self.block) > (self.block // 2):
                    n += 1
            elif self._run == 1 and not self.accurate_mode:
                n = max(self.step_blocks, 2)
            else:
                n = 1 << min(self._run, 16)
            n = min(self.readahead, n)
            missing = 0
            while missing < n and (b + missing) not in self.cache and (b + missing) * self.block < self.size:
                missing += 1
            self._fetch(b, max(1, missing))
        else:
            self.cache.move_to_end(b)
        self._last_block = b
        return self.cache[b]

    def readable(self):
        return True

    def seekable(self):
        return True

    def seek(self, off, whence=0):
        if whence == 0:
            self.pos = off
        elif whence == 1:
            self.pos += off
        elif whence == 2:
            self.pos = self.size + off
        self.pos = max(0, self.pos)
        return self.pos

    def tell(self):
        return self.pos

    def readinto(self, buf):
        if self.pos >= self.size:
            return 0
        b = self.pos // self.block
        data = self._get_block(b)
        o = self.pos - b * self.block
        n = min(len(buf), len(data) - o)
        if n <= 0:
            return 0
        buf[:n] = data[o:o + n]
        self.pos += n
        return n

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        super().close()


def fit_box(w: int, h: int, sar: float = 1.0, box_w: int = THUMB_W, box_h: int = THUMB_H) -> tuple[int, int]:
    """Largest size inside box_w x box_h (default 320x180) with the source's display aspect ratio."""
    dw = max(1.0, w * (sar if sar and sar > 0 else 1.0))
    s = min(box_w / dw, box_h / max(1, h))
    sw = max(2, min(box_w, int(round(dw * s))))
    sh = max(2, min(box_h, int(round(h * s))))
    return sw, sh


@dataclass
class MediaInfo:
    duration_ms: int
    width: int
    height: int
    codec: str
    hdr: bool
    container: str


class Extractor:
    """Persistent demuxer/decoder for one source. Not thread-safe."""

    def __init__(self, reader: ProxyReader, fmt: str, max_dim: int = 8192, max_duration_s: float = 12 * 3600,
                 nearest_kf: bool = False):
        import av  # imported only in the worker process

        self.nearest_kf = bool(nearest_kf)   # also consider the next keyframe (feature flag)

        self.av = av
        if fmt not in FORMAT_DEMUXER:
            raise EngineError("not_video", "format")
        self.reader = reader
        try:
            self.c = av.open(reader, mode="r", format=FORMAT_DEMUXER[fmt],
                             options={"probesize": "262144", "analyzeduration": "500000",
                                      # defence in depth: the demuxer is forced and fed from a Python file object, so it must
                                      # never open any protocol itself (e.g. an external data reference inside a hostile container)
                                      "protocol_whitelist": "none"})
        except EngineError:
            raise
        except Exception as e:  # noqa: BLE001 - libav error: not a usable container
            raise EngineError("not_video", type(e).__name__) from None
        name = (self.c.format.name or "")
        if not any(part in FORMAT_ALLOW for part in name.split(",")):
            raise EngineError("not_video", "demuxer")
        if not self.c.streams.video:
            raise EngineError("not_video", "no_video_stream")
        self.vs = self.c.streams.video[0]
        cc = self.vs.codec_context
        if cc is None or not getattr(cc, "name", None):
            raise EngineError("unsupported_codec", "no_decoder")
        w, h = int(cc.width or 0), int(cc.height or 0)
        if w <= 0 or h <= 0 or w > max_dim or h > max_dim:
            raise EngineError("unsupported_codec", "dimensions")
        self.vs.thread_type = "SLICE"   # frame threading over-reads ~1 packet per thread after a seek
        self.width, self.height = w, h
        self.tb = self.vs.time_base
        cstart = self.c.start_time  # microseconds (AV_TIME_BASE) or None
        self.offset_s = (cstart / 1e6) if cstart and cstart > 0 else 0.0
        dur = None
        if self.c.duration:
            dur = self.c.duration / 1e6
        elif self.vs.duration and self.tb:
            dur = float(self.vs.duration * self.tb)
        if not dur or dur <= 0:
            raise EngineError("not_video", "no_duration")
        if dur > max_duration_s:
            raise EngineError("not_video", "too_long")
        self.duration_ms = int(dur * 1000)
        trc = getattr(cc, "color_trc", None)
        self.hdr = trc in (TRC_PQ, TRC_HLG)
        self.trc = trc
        sar = self.vs.sample_aspect_ratio
        self.sar = float(sar) if sar else 1.0
        self._demux_iter = None
        self._last_pts_ms: int | None = None
        self._last_kf_entry_ts: int | None = None
        self._last_kf_result: tuple[int, bytes] | None = None
        self.thumb = (THUMB_W, THUMB_H, JPEG_QUALITY)
        if dur > 0 and getattr(self.reader, "size", 0) > 0 and hasattr(self.reader, "configure_for_bitrate"):
            self.reader.configure_for_bitrate(self.reader.size / dur)

    def set_thumb(self, w: int, h: int, q: int) -> None:
        """Tile geometry and JPEG quality for the following grabs (a quality preset; see quality.py)."""
        if (w, h, q) != self.thumb:
            self.thumb = (int(w), int(h), int(q))
            if self._last_kf_result is not None and self._last_kf_result[1]:
                self._last_kf_result = None          # a cached frame rendered at the previous geometry

    def info(self) -> MediaInfo:
        return MediaInfo(self.duration_ms, self.width, self.height, self.vs.codec_context.name, self.hdr,
                         self.c.format.name)

    def _pts_to_ms(self, pts: int) -> int:
        return max(0, int(round((float(pts * self.tb) - self.offset_s) * 1000)))

    def _lookup_index_entry(self, target_ts: int):
        try:
            entries = self.vs.index_entries
            if entries and len(entries) > 1:
                idx = entries.search_timestamp(target_ts, backward=True, any_frame=False)
                if idx >= 0:
                    return entries[idx]
        except Exception:  # noqa: BLE001
            pass
        return None

    def _forward_keyframe(self, entry, target_ts: int):
        """Next keyframe index entry after `entry`, or None."""
        try:
            entries = self.vs.index_entries
            n = len(entries)
            i = entries.search_timestamp(entry.timestamp, backward=False, any_frame=False) + 1
            while 0 < i < n:
                e = entries[i]
                if e.timestamp > entry.timestamp and getattr(e, "is_keyframe", True):
                    return e
                i += 1
        except Exception:  # noqa: BLE001
            pass
        return None

    def grab(self, t_ms: int, accurate: bool, max_packets: int = 4000,
             tolerance_ms: int = 5000, keep_open: bool = False) -> tuple[int, bytes]:
        t_ms = max(0, min(int(t_ms), self.duration_ms - 1))
        target = int((t_ms / 1000.0 + self.offset_s) / self.tb)
        if hasattr(self.reader, "accurate_mode"):
            self.reader.accurate_mode = bool(accurate or keep_open)

        entry = self._lookup_index_entry(target)
        entry_ts = entry.timestamp if entry is not None else None

        seek_ts = target
        if (self.nearest_kf and not accurate and entry is not None
                and abs(self._pts_to_ms(entry.timestamp) - t_ms) > tolerance_ms):
            nxt = self._forward_keyframe(entry, target)
            if nxt is not None and abs(self._pts_to_ms(nxt.timestamp) - t_ms) <= tolerance_ms:
                seek_ts = nxt.timestamp          # closer keyframe after the slot: no forward decode
                entry, entry_ts = nxt, nxt.timestamp

        # Fast path for multi-tile long GOPs: if accurate=False would seek back to the
        # exact same out-of-tolerance keyframe we already decoded while our forward
        # demuxer is still positioned before t_ms in that GOP, return the cached keyframe
        # result without seeking so the immediate accurate=True follow-up can continue
        # decoding forward from _last_pts_ms instead of re-decoding from the start of the GOP.
        if (not accurate and entry_ts is not None and entry_ts == self._last_kf_entry_ts
                and self._last_kf_result is not None and self._demux_iter is not None
                and self._last_pts_ms is not None and self._last_pts_ms <= t_ms):
            return self._last_kf_result

        reuse_forward = False
        if seek_ts == target and self._demux_iter is not None and self._last_pts_ms is not None:
            delta_ms = t_ms - self._last_pts_ms
            if accurate and 0 <= delta_ms <= 60_000:
                if entry_ts is None or self._last_kf_entry_ts is None or entry_ts == self._last_kf_entry_ts:
                    reuse_forward = True
            elif not accurate and entry is not None and 0 <= delta_ms <= 12_000:
                byte_gap = entry.pos - self.reader.pos
                if 0 <= byte_gap <= 2 * self.reader.block:
                    reuse_forward = True

        try:
            if not reuse_forward:
                self.c.seek(seek_ts, stream=self.vs, backward=True, any_frame=False)
                self._demux_iter = self.c.demux(self.vs)
                self._last_pts_ms = None
                if entry is None:
                    entry = self._lookup_index_entry(target)
                    entry_ts = entry.timestamp if entry is not None else None
                self._last_kf_entry_ts = entry_ts
                self._last_kf_result = None

            frame = None
            packets = 0
            flushed_keyframe = False
            cc = self.vs.codec_context
            for pkt in self._demux_iter:
                packets += 1
                if packets > max_packets:
                    break
                p_pts = pkt.pts if pkt.pts is not None else pkt.dts
                p_ms = self._pts_to_ms(p_pts) if p_pts is not None else None
                pkt_in_tol = (p_ms is not None and abs(p_ms - t_ms) <= tolerance_ms)
                if accurate and cc is not None:
                    try:
                        cc.skip_frame = "NONREF" if (p_ms is not None and p_ms < t_ms - 600) else "DEFAULT"
                    except Exception:  # noqa: BLE001
                        pass
                frames = pkt.decode()
                if (not accurate and not keep_open and packets == 1
                        and pkt.is_keyframe and p_ms is not None and not pkt_in_tol and not reuse_forward):
                    # Out-of-tolerance keyframe: keep _demux_iter open right after Packet 1 and
                    # return immediately without reading more packets or JPEG-encoding a throwaway frame,
                    # so the follow-up accurate=True call continues from Packet 2 with accurate_mode=True.
                    self._last_pts_ms = p_ms
                    res = (p_ms, b"")
                    self._last_kf_entry_ts = entry_ts if entry_ts is not None else p_pts
                    self._last_kf_result = res
                    return res
                if (not frames and not accurate and not keep_open
                        and pkt.is_keyframe and pkt_in_tol and not reuse_forward):
                    # Flush the B-frame reorder buffer on Packet 1 so we emit the IDR keyframe
                    # immediately without reading 1.5-2.5 MiB of subsequent interleaved audio/video.
                    frames = cc.decode(None)
                    cc.flush_buffers()
                    flushed_keyframe = True
                for f in frames:
                    if f.pts is None:
                        continue
                    f_ms = self._pts_to_ms(f.pts)
                    self._last_pts_ms = f_ms
                    if not accurate:
                        if reuse_forward and entry_ts is not None and f.pts < entry_ts and abs(f_ms - t_ms) > tolerance_ms:
                            continue
                        frame = f
                        break
                    frame = f
                    if f.pts >= target:
                        break
                else:
                    continue
                break
            if flushed_keyframe or frame is None:
                self._demux_iter = None
                self._last_pts_ms = None
        except EngineError:
            self._demux_iter = None
            self._last_pts_ms = None
            raise
        except Exception as e:  # noqa: BLE001
            self._demux_iter = None
            self._last_pts_ms = None
            raise EngineError("internal", "decode:" + type(e).__name__) from None
        finally:
            if self.vs.codec_context is not None:
                try:
                    self.vs.codec_context.skip_frame = "DEFAULT"
                except Exception:  # noqa: BLE001
                    pass
        if frame is None:
            raise EngineError("internal", "no_frame")
        pts_ms = self._pts_to_ms(frame.pts)
        res = (pts_ms, self._thumbnail(frame))
        if not accurate and not flushed_keyframe:
            self._last_kf_entry_ts = entry_ts if entry_ts is not None else frame.pts
            self._last_kf_result = res
        return res

    def keyframe_times(self, limit: int = 20000) -> list[int]:
        """Presentation times (ms) of the container's keyframes, ascending. Matroska loads its Cues on the
        first seek, so one seek to the middle is made when the index is still empty."""
        try:
            if len(self.vs.index_entries) <= 1:
                self.c.seek(int((self.duration_ms / 2000.0 + self.offset_s) / self.tb), stream=self.vs, backward=True)
                self._demux_iter = None
                self._last_pts_ms = None
            out = [self._pts_to_ms(e.timestamp) for e in self.vs.index_entries if getattr(e, "is_keyframe", True)]
        except Exception:  # noqa: BLE001
            return []
        out = sorted(set(out))
        return out[:limit]

    def keyframe_index(self, limit: int = 40000) -> list[tuple[int, int]]:
        """(presentation ms, byte position) of every keyframe the container index knows, ascending. For Matroska the
        position is the start of the Cluster that holds the keyframe (the extractor's own first read starts there)."""
        try:
            if len(self.vs.index_entries) <= 1:
                self.c.seek(int((self.duration_ms / 2000.0 + self.offset_s) / self.tb), stream=self.vs, backward=True)
                self._demux_iter = None
                self._last_pts_ms = None
            out = {(self._pts_to_ms(e.timestamp), int(e.pos)) for e in self.vs.index_entries if getattr(e, "is_keyframe", True)}
        except Exception:  # noqa: BLE001
            return []
        return sorted(out)[:limit]

    def grab_batch(self, targets_ms: list[int], tolerance_ms: int = 5000) -> list[tuple[int, int, bool, bytes]]:
        """Extract a sorted batch of target timestamps (t_ms), reusing forward decoder state
        whenever consecutive targets share a GOP or are within a small forward byte window.
        Returns list of (t_ms, pts_ms, accurate, jpeg)."""
        out: list[tuple[int, int, bool, bytes]] = []
        n = len(targets_ms)
        for i, raw_t in enumerate(targets_ms):
            t_ms = max(0, min(int(raw_t), self.duration_ms - 1))
            keep_open = False
            if i + 1 < n:
                nxt_ms = max(0, min(int(targets_ms[i + 1]), self.duration_ms - 1))
                if 0 <= nxt_ms - t_ms <= 30_000:
                    nxt_target = int((nxt_ms / 1000.0 + self.offset_s) / self.tb)
                    nxt_entry = self._lookup_index_entry(nxt_target)
                    cur_target = int((t_ms / 1000.0 + self.offset_s) / self.tb)
                    cur_entry = self._lookup_index_entry(cur_target)
                    if cur_entry is None or nxt_entry is None or nxt_entry.timestamp == cur_entry.timestamp:
                        keep_open = True
                    elif abs(self._pts_to_ms(nxt_entry.timestamp) - nxt_ms) > tolerance_ms:
                        keep_open = True
            pts_ms, jpg = self.grab(t_ms, accurate=False, tolerance_ms=tolerance_ms, keep_open=keep_open)
            acc = False
            if abs(pts_ms - t_ms) > tolerance_ms:
                acc = True
                pts_ms, jpg = self.grab(t_ms, accurate=True, tolerance_ms=tolerance_ms, keep_open=keep_open)
            out.append((int(raw_t), pts_ms, acc, jpg))
        return out

    def _thumbnail(self, frame) -> bytes:
        from PIL import Image

        sar = float(frame.sample_aspect_ratio) if getattr(frame, "sample_aspect_ratio", None) else self.sar
        tw, th, tq = self.thumb
        sw, sh = fit_box(frame.width, frame.height, sar, tw, th)
        if self.hdr:
            img = self._tonemap(frame, sw, sh)
        else:
            img = self._to_rgb(frame, sw, sh)
        canvas = Image.new("RGB", (tw, th))
        canvas.paste(img, ((tw - sw) // 2, (th - sh) // 2))
        out = io.BytesIO()
        canvas.save(out, "JPEG", quality=tq)
        return out.getvalue()

    def _to_rgb(self, frame, sw: int, sh: int):
        from PIL import Image

        kw = {}
        if getattr(frame, "colorspace", None) == 1 or (frame.height >= 720 and not getattr(frame, "colorspace", 0)):
            kw["src_colorspace"] = "ITU709"
        try:
            f = frame.reformat(width=sw, height=sh, format="rgb24", interpolation="BILINEAR", **kw)
        except Exception:  # noqa: BLE001 - older/odd colourspace metadata
            f = frame.reformat(width=sw, height=sh, format="rgb24", interpolation="BILINEAR")
        return Image.fromarray(f.to_ndarray(), "RGB")

    def _tonemap(self, frame, sw: int, sh: int):
        """PQ (or HLG) -> linear (100 nit = 1.0) -> Hable (peak 1000 nit) -> BT.2020->BT.709 -> gamma."""
        import numpy as np
        from PIL import Image

        f = frame.reformat(width=sw, height=sh, format="rgb48le", src_colorspace="BT2020",
                           interpolation="BILINEAR")
        e = f.to_ndarray().astype(np.float32) / 65535.0
        if self.trc == TRC_PQ:
            m1, m2 = 2610 / 16384, 2523 / 4096 * 128
            c1, c2, c3 = 3424 / 4096, 2413 / 4096 * 32, 2392 / 4096 * 32
            p = np.power(np.clip(e, 0, 1), 1 / m2)
            lin = np.power(np.maximum(p - c1, 0) / (c2 - c3 * p), 1 / m1) * 100.0
        else:
            a, b, c = 0.17883277, 0.28466892, 0.55991073
            lin = np.where(e <= 0.5, e * e / 3, (np.exp((e - c) / a) + b) / 12) * 10.0

        def hable(x):
            A, B, C, D, E, F = 0.15, 0.50, 0.10, 0.20, 0.02, 0.30
            return (x * (A * x + C * B) + D * E) / (x * (A * x + B) + D * F) - E / F

        tm = hable(lin) / hable(np.float32(10.0))
        m = np.array([[1.6605, -0.5876, -0.0728], [-0.1246, 1.1329, -0.0083], [-0.0182, -0.1006, 1.1187]],
                     dtype=np.float32)
        rgb = np.clip(tm @ m.T, 0, 1)
        out = (np.power(rgb, 1 / 2.4) * 255 + 0.5).astype(np.uint8)
        return Image.fromarray(out, "RGB")

    def close(self) -> None:
        try:
            self.c.close()
        finally:
            self.reader.close()

