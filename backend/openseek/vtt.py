"""WebVTT generation (CONTRACT-v1 "VTT semantics", SCRUBBING.md §3).

* Only published tiles appear. A tile with real pts p gets the cue [p - tol, p + tol), clipped
  at the midpoint between neighbouring published tiles' pts and at [0, duration].
* Cues are sorted and non-overlapping; positions outside every cue have no preview.
* Every URL is absolute (scheme/host/port the client used) and carries `&t=<pts_ms>` before
  the `#xywh` fragment. Layouts: tiles | sheets | hybrid (complete sheet blocks as sheets).
* Quality presets (quality.py): `#xywh` always uses the geometry and sheet grid of the image the cue points
  at. Non-Standard images carry `&q=<preset>` (Standard URLs are exactly the original ones), so a client never
  reuses an image of one quality with another quality's coordinates. Slots the active quality has not made yet
  may point at a tile cached at another quality (`fallback`), each with its own correct coordinates.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .quality import STANDARD, Quality

LAYOUTS = ("tiles", "sheets", "hybrid")


def ts(ms: int) -> str:
    ms = max(0, int(ms))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, frac = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{frac:03d}"


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    k: int
    pts_ms: int


def cues(tiles: dict[int, int], duration_ms: int | None, tolerance_ms: int) -> list[Cue]:
    """tiles: k -> pts_ms. Tiles sharing a pts keep the lowest k."""
    by_pts: dict[int, int] = {}
    for k, p in tiles.items():
        if p not in by_pts or k < by_pts[p]:
            by_pts[p] = k
    pts = sorted(by_pts)
    end_limit = duration_ms if duration_ms and duration_ms > 0 else None
    out = []
    for i, p in enumerate(pts):
        start = p - tolerance_ms
        end = p + tolerance_ms
        if i > 0:
            start = max(start, (pts[i - 1] + p) // 2)
        if i + 1 < len(pts):
            end = min(end, (p + pts[i + 1]) // 2)
        start = max(0, start)
        if end_limit is not None:
            end = min(end, end_limit)
        if end > start:
            out.append(Cue(start, end, by_pts[p], p))
    return out


def complete_sheets(tiles: dict[int, int], n_tiles: int | None, per_sheet: int = STANDARD.per_sheet) -> set[int]:
    if not n_tiles:
        return set()
    out = set()
    for n in range((n_tiles - 1) // per_sheet + 1):
        lo, hi = n * per_sheet, min(n_tiles, (n + 1) * per_sheet)
        if all(k in tiles for k in range(lo, hi)):
            out.add(n)
    return out


def render(base_url: str, cap: str, preview_id: str, exp: int, layout: str, tiles: dict[int, int],
           duration_ms: int | None, n_tiles: int | None, tolerance_ms: int,
           mapping: "tuple[float, float, int] | None" = None, quality: Quality = STANDARD,
           fallback: Sequence[tuple[Quality, dict[int, int]]] = ()) -> str:
    """mapping = (a, b, guest_duration_ms) for a borrowed preview: cue times are cache time; the guest sees
    t_guest = (t_cache - b) / a, clipped to the guest's own duration. Images and `t=` (cache pts) are unchanged.
    `tiles` are the tiles of `quality`; `fallback` = other qualities' tiles, in order of preference, used only for
    slots `tiles` lacks."""
    if layout not in LAYOUTS:
        raise ValueError("layout")
    prefix = f"{base_url}/v1/c/{cap}/{preview_id}"
    layers = [(quality, tiles)] + [(q, t) for q, t in fallback if q != quality]
    owner: dict[int, int] = {}                          # k -> index of the layer that serves it
    merged: dict[int, int] = {}
    for i, (_q, t) in enumerate(layers):
        for k, p in t.items():
            if k not in merged:
                merged[k], owner[k] = p, i
    fills: list[dict[int, int]] = []
    fulls: list[set[int]] = []
    for q, t in layers:
        fill: dict[int, int] = {}
        for k in t:
            fill[k // q.per_sheet] = fill.get(k // q.per_sheet, 0) + 1
        fills.append(fill)
        fulls.append(complete_sheets(t, n_tiles, q.per_sheet) if layout == "hybrid" else set())
    lines = ["WEBVTT", ""]
    for c in cues(merged, duration_ms, tolerance_ms):
        start_ms, end_ms = c.start_ms, c.end_ms
        if mapping is not None:
            a, b, gdur = mapping
            start_ms = max(0, int(round((c.start_ms - b) / a)))
            end_ms = min(int(gdur), int(round((c.end_ms - b) / a)))
            if end_ms <= start_ms:
                continue                                     # the cue falls outside the guest's timeline
        i = owner[c.k]
        q = layers[i][0]
        qp = "" if q == STANDARD else f"&q={q.name}"
        n = c.k // q.per_sheet
        if layout == "sheets" or (layout == "hybrid" and n in fulls[i]):
            slot = c.k % q.per_sheet
            x, y = (slot % q.cols) * q.w, (slot // q.cols) * q.h
            url = f"{prefix}/sheets/{n}.jpg?exp={exp}&v={fills[i][n]}{qp}&t={c.pts_ms}#xywh={x},{y},{q.w},{q.h}"
        else:
            url = f"{prefix}/tiles/{c.k}.jpg?exp={exp}{qp}&t={c.pts_ms}#xywh=0,0,{q.w},{q.h}"
        lines += [f"{ts(start_ms)} --> {ts(end_ms)}", url, ""]
    return "\n".join(lines)
