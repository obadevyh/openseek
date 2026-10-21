"""Thumbnail quality presets (owner setting `thumb_quality`, stored in the settings table).

Each preset fixes the tile geometry, the JPEG quality and the sprite-sheet grid. The grid keeps every sheet within
3200x1800, the largest image the Nuvio client accepts (and the size of a Standard sheet), so a higher preset never
grows the client's decoded-sheet memory. Standard is exactly the geometry OpenSeek always used: its cache path and
its URLs are unchanged, so existing caches keep working as they are.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("openseek.quality")

SETTING_KEY = "thumb_quality"


@dataclass(frozen=True)
class Quality:
    name: str
    label: str
    w: int
    h: int
    jpeg_q: int
    cols: int
    rows: int

    @property
    def per_sheet(self) -> int:
        return self.cols * self.rows

    @property
    def profile(self) -> str:
        """Cache path component: tile geometry/quality/grid/extractor version (Standard = the original layout)."""
        return f"t{self.w}x{self.h}q{self.jpeg_q}g10v1"

    @property
    def sheet_size(self) -> tuple[int, int]:
        return self.w * self.cols, self.h * self.rows

    def wire(self) -> list[int]:
        """What the worker needs to render a tile: [w, h, jpeg quality]."""
        return [self.w, self.h, self.jpeg_q]

    def public(self) -> dict:
        return {"name": self.name, "label": self.label, "width": self.w, "height": self.h, "jpeg_quality": self.jpeg_q,
                "sheet_grid": [self.cols, self.rows]}


LOW = Quality("low", "Low", 160, 90, 60, 10, 10)
STANDARD = Quality("standard", "Standard", 320, 180, 75, 10, 10)
HIGH = Quality("high", "High", 480, 270, 85, 6, 6)
MAX = Quality("max", "Max", 640, 360, 90, 5, 5)

PRESETS: tuple[Quality, ...] = (LOW, STANDARD, HIGH, MAX)
BY_NAME = {q.name: q for q in PRESETS}
BY_PROFILE = {q.profile: q for q in PRESETS}
BY_WIRE = {tuple(q.wire()): q for q in PRESETS}
DEFAULT = STANDARD


def parse(value: object, default: Quality = DEFAULT) -> Quality:
    """A preset by name (case-insensitive). Missing or unknown values fall back to `default` and never raise."""
    if isinstance(value, Quality):
        return value
    if isinstance(value, str) and value.strip().lower() in BY_NAME:
        return BY_NAME[value.strip().lower()]
    if value not in (None, ""):
        log.warning("thumbnail quality %r is not one of %s; using %s", value, "/".join(BY_NAME), default.name)
    return default


def from_wire(value: object) -> Quality | None:
    """The preset a worker request names, or None (only the presets' exact geometries are ever rendered)."""
    if isinstance(value, (list, tuple)) and len(value) == 3 and all(isinstance(x, int) and not isinstance(x, bool) for x in value):
        return BY_WIRE.get(tuple(value))
    return None
