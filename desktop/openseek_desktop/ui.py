from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (QAbstractListModel, QModelIndex, QObject, QPoint, QPointF, QRect, QRectF, QRunnable, QSize,
                            QSortFilterProxyModel, Qt, QThread, QThreadPool, QTimer, Signal, Slot)
from PySide6.QtGui import (QAction, QBrush, QColor, QFont, QFontMetrics, QGuiApplication, QIcon, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPen, QPixmap, QPixmapCache)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListView, QMainWindow, QMenu,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider, QStackedWidget,
                               QStyle, QStyledItemDelegate, QStyleOptionViewItem, QSystemTrayIcon, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import core, theme
from .core import Admin, Config, Entry, Library, LogTail, Metadata, ago, human_bytes, human_duration, redact
from .theme import ACCENT, ACCENT_2, BAD, BORDER, CARD, CARD_HI, FAINT, GOOD, MUTED, SURFACE, TEXT, WARN, qc


# ================================================================================================ helpers
def make_icon(size: int = 256) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    r = QRectF(size * 0.04, size * 0.04, size * 0.92, size * 0.92)
    g = QLinearGradient(r.topLeft(), r.bottomRight())
    g.setColorAt(0, QColor("#9b8cff"))
    g.setColorAt(1, QColor("#4fd1c1"))
    p.setBrush(QBrush(g))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(r, size * 0.24, size * 0.24)
    # a seek bar with a thumb: the thing OpenSeek makes
    p.setBrush(QColor(255, 255, 255, 235))
    bar = QRectF(size * 0.2, size * 0.52, size * 0.6, size * 0.07)
    p.drawRoundedRect(bar, size * 0.035, size * 0.035)
    p.setBrush(QColor(13, 14, 18, 230))
    p.drawEllipse(QPointF(size * 0.56, size * 0.555), size * 0.085, size * 0.085)
    p.setBrush(QColor(255, 255, 255, 235))
    card = QRectF(size * 0.4, size * 0.22, size * 0.32, size * 0.2)
    p.drawRoundedRect(card, size * 0.04, size * 0.04)
    p.end()
    return pm


def ui_font(px: int, weight=QFont.Normal) -> QFont:
    f = QFont("Segoe UI Variable Text")
    f.setPixelSize(px)
    f.setWeight(weight)
    return f


def card(widget_layout=None, flat=False) -> QFrame:
    f = QFrame()
    f.setObjectName("cardFlat" if flat else "card")
    return f


def label(text="", name=None, muted=False, wrap=False) -> QLabel:
    l = QLabel(text)
    if name:
        l.setObjectName(name)
    if muted:
        l.setObjectName("muted")
    l.setWordWrap(wrap)
    return l


class Toggle(QCheckBox):
    """A pill switch."""

    def __init__(self, text=""):
        super().__init__(text)
        self.setCursor(Qt.PointingHandCursor)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = 38, 20
        y = (self.height() - h) // 2
        on = self.isChecked()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(ACCENT) if on else QColor("#2b3040"))
        p.drawRoundedRect(QRectF(0, y, w, h), h / 2, h / 2)
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QPointF(w - 10 if on else 10, y + h / 2), 7, 7)
        p.setPen(QColor(TEXT))
        p.setFont(self.font())
        p.drawText(QRect(w + 10, 0, self.width() - w - 10, self.height()), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        p.end()

    def sizeHint(self):
        return QSize(46 + QFontMetrics(self.font()).horizontalAdvance(self.text()) + 6, 26)


class Pill(QLabel):
    def __init__(self, text="", color=MUTED):
        super().__init__(text)
        self.set(text, color)

    def set(self, text, color):
        self.setText(text)
        c = QColor(color)
        self.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},0.14); color: {color}; border-radius: 10px; "
                           f"padding: 3px 10px; font-size: 12px; font-weight: 600;")
        self.setAlignment(Qt.AlignCenter)


class Stat(QFrame):
    def __init__(self, caption: str):
        super().__init__()
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(2)
        self.value = label("–", "statValue")
        self.caption = label(caption, "statLabel")
        self.sub = label("", "faint")
        for w in (self.value, self.caption, self.sub):
            lay.addWidget(w)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setFixedHeight(6)
        self.bar.hide()
        lay.addWidget(self.bar)

    def set(self, value, sub="", bar: float | None = None):
        self.value.setText(value)
        self.sub.setText(sub)
        if bar is None:
            self.bar.hide()
        else:
            self.bar.show()
            self.bar.setValue(int(max(0, min(1, bar)) * 1000))


def page_header(title: str, sub: str = "") -> QWidget:
    w = QWidget()
    l = QVBoxLayout(w)
    l.setContentsMargins(0, 0, 0, 0)
    l.setSpacing(2)
    l.addWidget(label(title, "h1"))
    if sub:
        l.addWidget(label(sub, muted=True))
    return w


# ================================================================================================ background poller
class Snapshot(dict):
    pass


class Poller(QThread):
    """Polls the live server (health, admin status/metrics/sharing/devices) and reads the library, off the UI thread."""
    snap = Signal(object)
    entries = Signal(object)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg, self.admin, self.lib = cfg, Admin(cfg), Library(cfg)
        self._run = True
        self._sig = None

    def stop(self):
        self._run = False
        self.wait(3000)

    def run(self):
        n = 0
        while self._run:
            s = Snapshot()
            s["health"] = core.api_health(self.cfg)
            s["online"] = s["health"] is not None
            s["procs"] = len(core.server_processes(self.cfg)) if n % 3 == 0 else None
            for key, path in (("status", "/admin/status"), ("metrics", "/admin/metrics"), ("sharing", "/admin/sharing"),
                              ("contrib", "/admin/contributions"), ("thumbs", "/admin/thumbnails")):
                s[key] = self.admin.get(path)
            if n % 3 == 0:
                s["devices"] = self.admin.get("/admin/devices")
                s["invites"] = self.admin.get("/admin/invites")
            s["t"] = time.time()
            s["admin_auth_failed"] = self.admin.auth_failed
            self.snap.emit(s)
            try:
                es = self.lib.entries()
                sig = hash(tuple((e.fp, e.tiles, e.n_tiles, e.last_used, e.imdb, e.season, e.episode) for e in es))
                if sig != self._sig:
                    self._sig = sig
                    self.entries.emit(es)
            except Exception:  # noqa: BLE001 - a locked/missing database must never kill the poller
                pass
            n += 1
            for _ in range(20):
                if not self._run:
                    return
                self.msleep(100)


class MetaJob(QRunnable):
    def __init__(self, meta: Metadata, imdb: str, series: bool, done):
        super().__init__()
        self.meta, self.imdb, self.series, self.done = meta, imdb, series, done

    def run(self):
        m = self.meta.get(self.imdb, self.series)
        poster = self.meta.poster_path(m) if m else None
        self.done(self.imdb, m, str(poster) if poster else "")


class MetaBridge(QObject):
    ready = Signal(str, object, str)


# ================================================================================================ library model
@dataclass
class Card:
    key: str
    kind: str                       # movie | show | unknown
    entries: list[Entry]
    imdb: str | None = None
    title: str = ""
    year: str = ""
    meta: dict | None = None
    poster: str = ""

    @property
    def tiles(self): return sum(e.tiles for e in self.entries)
    @property
    def total(self): return sum(max(e.n_tiles, e.tiles) for e in self.entries)
    @property
    def size(self): return sum(e.bytes for e in self.entries)
    @property
    def last_used(self): return max(e.last_used for e in self.entries)
    @property
    def complete(self): return all(e.complete for e in self.entries)
    @property
    def pct(self): return (self.tiles / self.total) if self.total else 0.0
    @property
    def duration(self): return max(e.duration_ms for e in self.entries)


def build_cards(entries: list[Entry], metas: dict[str, tuple[dict | None, str]]) -> list[Card]:
    cards: dict[str, Card] = {}
    for e in entries:
        if e.imdb and e.is_episode:
            k = "show:" + e.imdb
            kind = "show"
        elif e.imdb:
            k = "movie:" + e.imdb + ":" + e.fp[:8] if False else "movie:" + e.imdb
            kind = "movie"
        else:
            k = "fp:" + e.fp
            kind = "unknown"
        c = cards.get(k)
        if c is None:
            c = cards[k] = Card(k, kind, [], e.imdb)
        c.entries.append(e)
    for c in cards.values():
        m = metas.get(c.imdb or "", (None, ""))
        c.meta, c.poster = m
        if c.meta:
            c.title = c.meta.get("name", "")
            c.year = str(c.meta.get("releaseInfo") or c.meta.get("year") or "")[:4]
        if not c.title:
            if c.kind == "unknown":
                e = c.entries[0]
                c.title = f"Unidentified · {human_duration(e.duration_ms)}"
            else:
                c.title = c.imdb or "?"
        c.entries.sort(key=lambda e: (e.season, e.episode, e.fp))
    return list(cards.values())


class CardModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.cards: list[Card] = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.cards)

    def data(self, idx, role=Qt.DisplayRole):
        if not idx.isValid():
            return None
        c = self.cards[idx.row()]
        if role == Qt.DisplayRole:
            return c.title
        if role == Qt.UserRole:
            return c
        return None

    def set_cards(self, cards: list[Card]):
        self.beginResetModel()
        self.cards = cards
        self.endResetModel()


class CardFilter(QSortFilterProxyModel):
    def __init__(self):
        super().__init__()
        self.query, self.mode, self.sort_key = "", "all", "recent"

    def filterAcceptsRow(self, row, parent):
        c: Card = self.sourceModel().cards[row]
        if self.query and self.query.lower() not in (c.title + " " + (c.imdb or "") + " " + c.entries[0].fp[:12]).lower():
            return False
        m = self.mode
        return not ((m == "movie" and c.kind != "movie") or (m == "show" and c.kind != "show") or
                    (m == "unknown" and c.kind != "unknown") or (m == "partial" and c.complete))

    def lessThan(self, a, b):
        x, y = self.sourceModel().cards[a.row()], self.sourceModel().cards[b.row()]
        if self.sort_key == "name":
            return x.title.lower() > y.title.lower()          # inverted: the view sorts descending
        if self.sort_key == "size":
            return x.size < y.size
        if self.sort_key == "thumbs":
            return x.tiles < y.tiles
        return x.last_used < y.last_used


CARD_W, CARD_H, POSTER_H = 176, 292, 232


def pix_from_file(path: str, w: int, h: int) -> QPixmap | None:
    key = f"{path}|{w}x{h}"
    pm = QPixmapCache.find(key)
    if pm:
        return pm
    img = QImage(path)
    if img.isNull():
        return None
    img = img.scaled(w, h, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
    x, y = (img.width() - w) // 2, (img.height() - h) // 2
    pm = QPixmap.fromImage(img.copy(x, y, w, h))
    QPixmapCache.insert(key, pm)
    return pm


class CardDelegate(QStyledItemDelegate):
    def __init__(self, lib: Library):
        super().__init__()
        self.lib = lib

    def sizeHint(self, opt, idx):
        return QSize(CARD_W + 14, CARD_H + 14)

    def paint(self, p: QPainter, opt: QStyleOptionViewItem, idx: QModelIndex):
        c: Card = idx.data(Qt.UserRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(7, 7, -7, -7)
        hover = bool(opt.state & QStyle.State_MouseOver) or bool(opt.state & QStyle.State_Selected)
        if hover:
            p.setPen(Qt.NoPen)
            p.setBrush(qc(CARD_HI))
            p.drawRoundedRect(r.adjusted(-5, -5, 5, 5), 16, 16)
        poster = QRectF(r.left(), r.top(), CARD_W, POSTER_H)
        clip = QPainterPath()
        clip.addRoundedRect(poster, 12, 12)
        p.setClipPath(clip)
        pm = None
        if c.poster:
            pm = pix_from_file(c.poster, CARD_W, POSTER_H)
        framed = False
        if pm is None:
            f = self.lib.poster_frame(c.entries[0])
            if f:
                pm = pix_from_file(str(f), CARD_W, POSTER_H)
                framed = True
        if pm is not None:
            p.drawPixmap(poster.toRect(), pm)
            if framed:
                p.fillRect(poster, qc("#000000", 70))
        else:
            g = QLinearGradient(poster.topLeft(), poster.bottomRight())
            hh = int(hashlib.md5(c.key.encode()).hexdigest()[:4], 16) % 360
            g.setColorAt(0, QColor.fromHsv(hh, 90, 70))
            g.setColorAt(1, QColor.fromHsv((hh + 40) % 360, 120, 40))
            p.fillRect(poster, QBrush(g))
        # bottom fade for legibility of the chips
        fade = QLinearGradient(poster.bottomLeft(), poster.bottomLeft() - QPointF(0, 70))
        fade.setColorAt(0, qc("#000000", 190))
        fade.setColorAt(1, qc("#000000", 0))
        p.fillRect(QRectF(poster.left(), poster.bottom() - 70, CARD_W, 70), QBrush(fade))
        p.setClipping(False)
        # status chip
        if c.complete:
            txt, col = "Complete", GOOD
        elif c.tiles == 0:
            txt, col = "Empty", BAD
        else:
            txt, col = f"{int(c.pct * 100)}%  partial", WARN
        self._chip(p, QPointF(poster.left() + 8, poster.bottom() - 28), txt, col)
        if c.kind == "show":
            self._chip(p, QPointF(poster.left() + 8, poster.top() + 8), f"{len(c.entries)} ep", ACCENT)
        elif c.kind == "unknown":
            self._chip(p, QPointF(poster.left() + 8, poster.top() + 8), "Untagged", FAINT)
        # text
        tr = QRectF(r.left() + 2, poster.bottom() + 9, CARD_W - 4, 22)
        p.setPen(QColor(TEXT))
        p.setFont(ui_font(13, QFont.DemiBold))
        p.drawText(tr, Qt.AlignLeft | Qt.AlignVCenter, QFontMetrics(p.font()).elidedText(c.title, Qt.ElideRight, int(tr.width())))
        sub = " · ".join(x for x in (c.year, f"{c.tiles:,} thumbs", ago(c.last_used)) if x)
        p.setPen(QColor(MUTED))
        p.setFont(ui_font(11))
        p.drawText(QRectF(tr.left(), tr.bottom() + 1, tr.width(), 18), Qt.AlignLeft | Qt.AlignVCenter,
                   QFontMetrics(p.font()).elidedText(sub, Qt.ElideRight, int(tr.width())))
        p.restore()

    @staticmethod
    def _chip(p: QPainter, at: QPointF, text: str, color: str):
        p.setFont(ui_font(10, QFont.DemiBold))
        fm = QFontMetrics(p.font())
        w = fm.horizontalAdvance(text) + 28
        rr = QRectF(at.x(), at.y(), w, 20)
        c = QColor(color)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 11, 14, 215))
        p.drawRoundedRect(rr, 10, 10)
        p.setBrush(c)
        p.drawEllipse(QPointF(rr.left() + 9, rr.center().y()), 3, 3)
        p.setPen(QColor(TEXT))
        p.drawText(rr.adjusted(17, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)


# ================================================================================================ tiles grid (detail)
class TileModel(QAbstractListModel):
    def __init__(self):
        super().__init__()
        self.files: list[tuple[int, Path]] = []

    def rowCount(self, parent=QModelIndex()):
        return len(self.files)

    def data(self, idx, role=Qt.DisplayRole):
        if role == Qt.UserRole:
            return self.files[idx.row()]
        return None

    def set_files(self, files):
        self.beginResetModel()
        self.files = files
        self.endResetModel()


class TileDelegate(QStyledItemDelegate):
    def sizeHint(self, opt, idx):
        return QSize(172, 112)

    def paint(self, p, opt, idx):
        k, path = idx.data(Qt.UserRole)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(opt.rect).adjusted(4, 4, -4, -22)
        clip = QPainterPath()
        clip.addRoundedRect(r, 8, 8)
        p.setClipPath(clip)
        pm = pix_from_file(str(path), int(r.width()), int(r.height()))
        if pm:
            p.drawPixmap(r.toRect(), pm)
        p.setClipping(False)
        p.setPen(QColor(MUTED))
        p.setFont(ui_font(11))
        secs = k * 10                                         # the tile grid is 10 s
        t = f"{secs // 3600}:{secs % 3600 // 60:02d}:{secs % 60:02d}" if secs >= 3600 else f"{secs // 60}:{secs % 60:02d}"
        p.drawText(QRectF(r.left(), r.bottom() + 2, r.width(), 16), Qt.AlignHCenter | Qt.AlignVCenter, t)
        p.restore()


# ================================================================================================ pages
class DashboardPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(18)
        top = QHBoxLayout()
        top.addWidget(page_header("Dashboard"))
        top.addStretch()
        self.pill = Pill("Checking…", MUTED)
        top.addWidget(self.pill)
        self.b_start, self.b_stop, self.b_restart = QPushButton("Start"), QPushButton("Stop"), QPushButton("Restart")
        self.b_start.setObjectName("primary")
        self.b_stop.setObjectName("danger")
        for b in (self.b_start, self.b_restart, self.b_stop):
            b.setCursor(Qt.PointingHandCursor)
            top.addWidget(b)
        lay.addLayout(top)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()
        lay.addWidget(self.banner)
        tv = card()
        tvl = QHBoxLayout(tv)
        tvl.setContentsMargins(20, 16, 20, 16)
        tvt = QVBoxLayout()
        tvt.setSpacing(3)
        tvt.addWidget(label("Connect a TV", "h2"))
        tvt.addWidget(label("Pair your TV with this server in a minute. OpenSeek shows the address and a one-time code to enter on the TV.", muted=True, wrap=True))
        tvl.addLayout(tvt, 1)
        self.b_tv = QPushButton("Connect a TV")
        self.b_tv.setObjectName("primary")
        self.b_tv.setCursor(Qt.PointingHandCursor)
        self.b_tv.setMinimumHeight(40)
        self.b_tv.setMinimumWidth(170)
        tvl.addWidget(self.b_tv)
        lay.addWidget(tv)
        grid = QGridLayout()
        grid.setSpacing(14)
        self.s_titles, self.s_thumbs, self.s_store, self.s_jobs = Stat("Cached titles"), Stat("Thumbnails"), Stat("Cache storage"), Stat("Active extraction")
        for i, s in enumerate((self.s_titles, self.s_thumbs, self.s_store, self.s_jobs)):
            grid.addWidget(s, 0, i)
        lay.addLayout(grid)
        feats = card()
        self.feats = feats
        fl = QHBoxLayout(feats)
        fl.setContentsMargins(18, 14, 18, 14)
        self.f_guard, self.f_multi, self.f_share, self.f_public = Pill(), Pill(), Pill(), Pill()
        for cap, w in (("Playback guard", self.f_guard), ("Multi-range", self.f_multi), ("Public sharing", self.f_share), ("Public endpoint", self.f_public)):
            col = QVBoxLayout()
            col.setSpacing(5)
            col.addWidget(label(cap, "statLabel"))
            col.addWidget(w, 0, Qt.AlignLeft)
            fl.addLayout(col)
            fl.addSpacing(26)
        fl.addStretch()
        lay.addWidget(feats)
        act = card()
        al = QVBoxLayout(act)
        al.setContentsMargins(18, 14, 18, 14)
        al.addWidget(label("Recent activity", "h2"))
        self.activity = QVBoxLayout()
        self.activity.setSpacing(6)
        al.addLayout(self.activity)
        al.addStretch()
        lay.addWidget(act)
        lay.addStretch(1)
        self.b_start.clicked.connect(win.do_start)
        self.b_stop.clicked.connect(win.do_stop)
        self.b_restart.clicked.connect(win.do_restart)
        self.b_tv.clicked.connect(win.connect_tv)

    def update_snapshot(self, s: Snapshot, entries: list[Entry]):
        online = s["online"]
        self.pill.set("● Online" if online else "● Offline", GOOD if online else BAD)
        self.b_start.setEnabled(not online)
        self.b_stop.setEnabled(online)
        self.b_restart.setEnabled(online)
        st, me, sh = s.get("status"), s.get("metrics"), s.get("sharing")
        n_t = sum(e.tiles for e in entries)
        total_b = sum(e.bytes for e in entries)
        cards = {e.imdb + str(e.season > 0) if e.imdb else e.fp for e in entries}
        self.s_titles.set(f"{len(cards)}", f"{len(entries)} cache entries")
        self.s_thumbs.set(f"{n_t:,}", f"{sum(1 for e in entries if e.complete)} of {len(entries)} entries complete")
        mx = (st or {}).get("cache", {}).get("max_bytes")
        if mx:
            self.s_store.set(human_bytes(total_b), f"of {human_bytes(mx)} limit", total_b / mx)
        else:
            self.s_store.set(human_bytes(total_b), "")
        active = (st or {}).get("active_sources")
        if active is None:
            self.s_jobs.set("–", "server offline")
        else:
            srcs = [x for x in (me or {}).get("sources", []) if x.get("running")]
            self.s_jobs.set(str(active), "idle" if not active else f"{sum(x.get('done', 0) for x in srcs)}/{sum(x.get('total', 0) for x in srcs)} thumbnails")
        cfg = (me or {}).get("config", {})
        self.f_guard.set("On" if cfg.get("playback_guard") else "Off", GOOD if cfg.get("playback_guard") else MUTED) if cfg else self.f_guard.set("–", MUTED)
        self.f_multi.set("On" if cfg.get("multirange_prefetch") else "Off", GOOD if cfg.get("multirange_prefetch") else MUTED) if cfg else self.f_multi.set("–", MUTED)
        if sh:
            self.f_share.set("On" if sh.get("enabled") else "Off", GOOD if sh.get("enabled") else MUTED)
            pub = sh.get("public", {})
            self.f_public.set(pub.get("host") if pub.get("listening") else "Not listening", ACCENT_2 if pub.get("listening") else MUTED)
        else:
            self.f_share.set("–", MUTED)
            self.f_public.set("–", MUTED)
        if not online:
            self.banner_msg("OpenSeek is not running on port %d. Press Start to launch it (your cache and pairings are untouched)." % self.win.cfg.api_port, WARN)
        elif st is None:
            self.banner_msg("The server answers, but the admin API (127.0.0.1:%d) does not. Library statistics come from the database directly; controls are limited." % self.win.cfg.admin_port, WARN)
        else:
            self.banner.hide()

    def banner_msg(self, text, color):
        c = QColor(color)
        self.banner.setText(text)
        self.banner.setStyleSheet(f"background: rgba({c.red()},{c.green()},{c.blue()},0.12); color: {color}; border-radius: 10px; padding: 10px 14px;")
        self.banner.show()

    def set_activity(self, items: list[tuple[str, str, str]]):
        while self.activity.count():
            w = self.activity.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not items:
            self.activity.addWidget(label("Nothing yet.", "faint"))
        for ts, text, color in items:
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color}; font-size: 8px;")
            h.addWidget(dot)
            t = label(text)
            h.addWidget(t, 1)
            h.addWidget(label(ts, "faint"))
            self.activity.addWidget(row)


class LibraryPage(QWidget):
    open_card = Signal(object)

    def __init__(self, win):
        super().__init__()
        self.win = win
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 0)
        lay.setSpacing(14)
        top = QHBoxLayout()
        top.addWidget(page_header("Library"))
        top.addStretch()
        self.count = label("", muted=True)
        top.addWidget(self.count)
        lay.addLayout(top)
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search titles…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(260)
        self.mode = QComboBox()
        for k, t in (("all", "All"), ("movie", "Movies"), ("show", "Series"), ("unknown", "Untagged"), ("partial", "Partial only")):
            self.mode.addItem(t, k)
        self.sort = QComboBox()
        for k, t in (("recent", "Recently used"), ("name", "Name"), ("size", "Size"), ("thumbs", "Thumbnails")):
            self.sort.addItem(t, k)
        bar.addWidget(self.search)
        bar.addWidget(self.mode)
        bar.addWidget(self.sort)
        bar.addStretch()
        lay.addLayout(bar)
        self.model = CardModel()
        self.proxy = CardFilter()
        self.proxy.setSourceModel(self.model)
        self.proxy.setDynamicSortFilter(True)
        self.view = QListView()
        self.view.setModel(self.proxy)
        self.view.setViewMode(QListView.IconMode)
        self.view.setResizeMode(QListView.Adjust)
        self.view.setMovement(QListView.Static)
        self.view.setUniformItemSizes(True)
        self.view.setSpacing(2)
        self.view.setMouseTracking(True)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.view.verticalScrollBar().setSingleStep(24)
        self.view.setItemDelegate(CardDelegate(win.lib))
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setFrameShape(QFrame.NoFrame)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.view, 1)
        self.empty = label("", muted=True)
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.hide()
        lay.addWidget(self.empty)
        self.search.textChanged.connect(self._filter)
        self.mode.currentIndexChanged.connect(self._filter)
        self.sort.currentIndexChanged.connect(self._filter)
        self.view.clicked.connect(lambda i: self.open_card.emit(i.data(Qt.UserRole)))
        self.view.doubleClicked.connect(lambda i: self.open_card.emit(i.data(Qt.UserRole)))
        self._filter()

    def _menu(self, pos: QPoint):
        i = self.view.indexAt(pos)
        if not i.isValid():
            return
        m = QMenu(self)
        m.addAction("Open", lambda: self.open_card.emit(i.data(Qt.UserRole)))
        m.exec(self.view.viewport().mapToGlobal(pos))

    def _filter(self):
        self.proxy.query = self.search.text().strip()
        self.proxy.mode = self.mode.currentData()
        self.proxy.sort_key = self.sort.currentData()
        self.proxy.invalidateFilter()
        self.proxy.sort(0, Qt.DescendingOrder)
        self.proxy.invalidate()
        n, tot = self.proxy.rowCount(), self.model.rowCount()
        self.count.setText(f"{n} of {tot} titles" if n != tot else f"{tot} titles")
        self.empty.setVisible(n == 0)
        self.empty.setText("No cached titles yet.\nPlay something in Nuvio and thumbnails will appear here." if tot == 0 else "Nothing matches.")

    def set_cards(self, cards: list[Card]):
        sb = self.view.verticalScrollBar().value()
        self.model.set_cards(cards)
        self._filter()
        self.view.verticalScrollBar().setValue(sb)


class DetailPage(QWidget):
    back = Signal()

    def __init__(self, win):
        super().__init__()
        self.win, self.card = win, None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 22, 32, 16)
        lay.setSpacing(12)
        top = QHBoxLayout()
        b = QPushButton("← Library")
        b.setObjectName("ghost")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(self.back.emit)
        top.addWidget(b)
        top.addStretch()
        lay.addLayout(top)
        head = QHBoxLayout()
        head.setSpacing(20)
        self.poster = QLabel()
        self.poster.setFixedSize(140, 210)
        head.addWidget(self.poster, 0, Qt.AlignTop)
        info = QVBoxLayout()
        self.title = label("", "h1")
        self.title.setWordWrap(True)
        self.subtitle = label("", muted=True)
        self.overview = label("", muted=True, wrap=True)
        self.overview.setMaximumHeight(64)
        self.facts = QHBoxLayout()
        info.addWidget(self.title)
        info.addWidget(self.subtitle)
        info.addWidget(self.overview)
        info.addLayout(self.facts)
        actions = QHBoxLayout()
        self.b_identify = QPushButton("Identify…")
        self.b_delete = QPushButton("Delete from cache…")
        self.b_delete.setObjectName("danger")
        actions.addWidget(self.b_identify)
        actions.addWidget(self.b_delete)
        actions.addStretch()
        info.addSpacing(6)
        info.addLayout(actions)
        info.addStretch()
        head.addLayout(info, 1)
        lay.addLayout(head)
        body = QHBoxLayout()
        body.setSpacing(16)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Title", "Length", "Thumbnails", "Size", "Last used"])
        self.tree.setRootIsDecorated(False)
        self.tree.setMinimumWidth(430)
        self.tree.setMaximumWidth(560)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            self.tree.header().setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._select)
        body.addWidget(self.tree)
        right = QVBoxLayout()
        self.tile_caption = label("", muted=True)
        right.addWidget(self.tile_caption)
        self.tiles = TileModel()
        self.tile_view = QListView()
        self.tile_view.setModel(self.tiles)
        self.tile_view.setViewMode(QListView.IconMode)
        self.tile_view.setResizeMode(QListView.Adjust)
        self.tile_view.setMovement(QListView.Static)
        self.tile_view.setUniformItemSizes(True)
        self.tile_view.setItemDelegate(TileDelegate())
        self.tile_view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.tile_view.setFrameShape(QFrame.NoFrame)
        right.addWidget(self.tile_view, 1)
        body.addLayout(right, 1)
        lay.addLayout(body, 1)
        self.b_identify.clicked.connect(self.identify)
        self.b_delete.clicked.connect(self.delete)

    def show_card(self, c: Card):
        self.card = c
        self.title.setText(c.title + (f"  ({c.year})" if c.year else ""))
        kind = {"show": f"Series · {len(c.entries)} cached episode(s)", "movie": "Movie", "unknown": "Not identified yet · no IMDb/TMDB id stored"}[c.kind]
        self.subtitle.setText(kind + (f" · {c.imdb}" if c.imdb else ""))
        self.overview.setText((c.meta or {}).get("description", "") if c.meta else "")
        pm = pix_from_file(c.poster, 140, 210) if c.poster else None
        if pm is None:
            f = self.win.lib.poster_frame(c.entries[0])
            pm = pix_from_file(str(f), 140, 210) if f else None
        self.poster.setPixmap(self._round(pm) if pm else QPixmap())
        while self.facts.count():
            w = self.facts.takeAt(0).widget()
            if w:
                w.deleteLater()
        self.facts.addWidget(Pill("Complete" if c.complete else f"Partial · {int(c.pct * 100)}%", GOOD if c.complete else WARN))
        self.facts.addWidget(Pill(f"{c.tiles:,} thumbnails", MUTED))
        self.facts.addWidget(Pill(human_bytes(c.size), MUTED))
        self.facts.addStretch()
        self.b_identify.setVisible(True)
        self.b_identify.setText("Identify…" if c.kind == "unknown" else "Re-identify…")
        self.tree.clear()
        seasons: dict[int, QTreeWidgetItem] = {}
        first = None
        for e in c.entries:
            parent = self.tree.invisibleRootItem()
            if c.kind == "show":
                if e.season not in seasons:
                    si = QTreeWidgetItem([f"Season {e.season}" if e.season else "Specials"])
                    self.tree.addTopLevelItem(si)
                    si.setExpanded(True)
                    seasons[e.season] = si
                parent = seasons[e.season]
                ename = Metadata.episode_name(c.meta, e.season, e.episode)
                name = f"E{e.episode:02d}" + (f" · {ename}" if ename else "")
            else:
                name = "This cache entry" if c.kind == "unknown" else "Cached file"
            it = QTreeWidgetItem([name, human_duration(e.duration_ms), f"{e.tiles}/{e.n_tiles}" + ("" if e.complete else "  partial"), human_bytes(e.bytes), ago(e.last_used)])
            it.setData(0, Qt.UserRole, e)
            if not e.complete:
                it.setForeground(2, QColor(WARN))
            parent.addChild(it)
            first = first or it
        if first:
            self.tree.setCurrentItem(first)

    @staticmethod
    def _round(pm: QPixmap) -> QPixmap:
        out = QPixmap(pm.size())
        out.fill(Qt.transparent)
        p = QPainter(out)
        p.setRenderHint(QPainter.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(QRectF(out.rect()), 12, 12)
        p.setClipPath(path)
        p.drawPixmap(0, 0, pm)
        p.end()
        return out

    def current_entry(self) -> Entry | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it else None

    def _select(self):
        e = self.current_entry()
        if e is None:
            self.tiles.set_files([])
            self.tile_caption.setText("")
            return
        files = self.win.lib.tile_files(e.fp)
        self.tiles.set_files(files)
        self.tile_caption.setText(f"{len(files):,} thumbnail file(s) on disk · every 10 s · read from the cache folder" if files else "No thumbnail files on disk for this entry.")

    def identify(self):
        e = self.current_entry()
        if e:
            IdentifyDialog(self.win, e).exec()

    def delete(self):
        e = self.current_entry()
        if not e:
            return
        c = self.card
        n = len(c.entries) if c.kind != "show" else 1
        what = f"this cached item ({e.tiles} thumbnails, {human_bytes(e.bytes)})"
        if QMessageBox.question(self, "Delete from cache", f"Delete {what}?\n\nOnly the thumbnails OpenSeek made are removed. "
                                "Your videos are not touched, and the thumbnails can be regenerated by playing the title again.\n\n"
                                "Guests who borrowed this cache entry lose it too.",
                                QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        r = self.win.admin.call("DELETE", f"/admin/library/entries/{e.fp}")
        if r is None:
            QMessageBox.warning(self, "Not deleted", "The server is not reachable, so nothing was deleted.")
        elif r.get("_error") == "in_use":
            QMessageBox.warning(self, "In use", "This title is being extracted or played right now. Try again when it is idle.")
        elif r.get("_error"):
            QMessageBox.warning(self, "Not deleted", "The server refused: " + str(r["_error"]) + "\n(If this says not_found, restart OpenSeek once to enable deletion from the app.)")
        else:
            self.win.toast("Deleted from cache.")
            QPixmapCache.clear()
            self.back.emit()


class IdentifyDialog(QDialog):
    """Attach an IMDb id to an existing cache entry. Only ids are stored; no thumbnail is touched."""

    def __init__(self, win, e: Entry):
        super().__init__(win)
        self.win, self.e = win, e
        self.setWindowTitle("Identify cache entry")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 20, 22, 18)
        lay.setSpacing(10)
        lay.addWidget(label("Identify this entry", "h2"))
        lay.addWidget(label(f"{human_duration(e.duration_ms)} · {e.tiles} thumbnails. Search a title, or paste an IMDb id. "
                            "Only the id is stored; thumbnails are not regenerated.", muted=True, wrap=True))
        row = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Search title (e.g. Breaking Bad)")
        go = QPushButton("Search")
        row.addWidget(self.q, 1)
        row.addWidget(go)
        lay.addLayout(row)
        self.results = QTreeWidget()
        self.results.setHeaderHidden(True)
        self.results.setMinimumHeight(150)
        lay.addWidget(self.results)
        r2 = QHBoxLayout()
        self.imdb = QLineEdit(e.imdb or "")
        self.imdb.setPlaceholderText("tt1234567")
        self.season = QLineEdit(str(e.season or ""))
        self.season.setPlaceholderText("Season")
        self.season.setFixedWidth(80)
        self.episode = QLineEdit(str(e.episode or ""))
        self.episode.setPlaceholderText("Episode")
        self.episode.setFixedWidth(80)
        for w in (self.imdb, self.season, self.episode):
            r2.addWidget(w)
        lay.addLayout(r2)
        lay.addWidget(label("Leave season/episode empty for a movie.", "faint"))
        btns = QHBoxLayout()
        btns.addStretch()
        cancel, save = QPushButton("Cancel"), QPushButton("Save")
        save.setObjectName("primary")
        btns.addWidget(cancel)
        btns.addWidget(save)
        lay.addLayout(btns)
        go.clicked.connect(self.search)
        self.q.returnPressed.connect(self.search)
        self.results.itemClicked.connect(lambda it, _: self.imdb.setText(it.data(0, Qt.UserRole)))
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.save)

    def search(self):
        self.results.clear()
        for m in Metadata.search(self.q.text().strip()):
            it = QTreeWidgetItem([f"{m['name']}  {m['year']}  ·  {m['type']}  ·  {m['id']}"])
            it.setData(0, Qt.UserRole, m["id"])
            self.results.addTopLevelItem(it)
        if not self.results.topLevelItemCount():
            self.results.addTopLevelItem(QTreeWidgetItem(["No results (or offline). You can still paste an IMDb id below."]))

    def save(self):
        imdb = self.imdb.text().strip()
        try:
            s = int(self.season.text() or 0)
            ep = int(self.episode.text() or 0)
        except ValueError:
            QMessageBox.warning(self, "Check the numbers", "Season and episode must be numbers.")
            return
        r = self.win.admin.call("PUT", f"/admin/library/entries/{self.e.fp}", {"imdb_id": imdb, "season": s, "episode": ep})
        if r is None:
            QMessageBox.warning(self, "Not saved", "The server is not reachable.")
        elif r.get("_error"):
            QMessageBox.warning(self, "Not saved", f"The server refused ({r['_error']}).\nIf it says not_found, restart OpenSeek once to enable identifying from the app.")
        else:
            self.win.toast("Saved. The title now appears with its real name.")
            self.accept()
            self.win.refresh_library_soon()


class ActivityPage(QWidget):
    def __init__(self, win):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(14)
        lay.addWidget(page_header("Live activity", "Real figures from the running server."))
        self.guard = label("", muted=True)
        lay.addWidget(self.guard)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setFrameShape(QFrame.NoFrame)
        sc.viewport().setAutoFillBackground(False)
        host = QWidget()
        host.setAutoFillBackground(False)
        self.col = QVBoxLayout(host)
        self.col.setContentsMargins(0, 0, 0, 0)
        self.col.setSpacing(12)
        self.col.addStretch()
        sc.setWidget(host)
        lay.addWidget(sc, 1)
        self.prev: dict[str, tuple[float, int]] = {}

    def update_snapshot(self, s: Snapshot, entries: list[Entry]):
        me = s.get("metrics")
        while self.col.count() > 1:
            w = self.col.takeAt(0).widget()
            if w:
                w.deleteLater()
        if not s["online"] or me is None:
            self.guard.setText("Server offline or admin API unreachable." if not s["online"] else "Admin metrics unavailable.")
            return
        cfg = me.get("config", {})
        self.guard.setText(f"Playback guard {'on' if cfg.get('playback_guard') else 'off'} · multi-range {'on' if cfg.get('multirange_prefetch') else 'off'} · "
                           f"{cfg.get('workers_per_source', '?')} worker(s) per source · {cfg.get('upstream_rps', '?')} requests/s cap")
        srcs = me.get("sources", [])
        running = [x for x in srcs if x.get("running")]
        done_srcs = [x for x in srcs if not x.get("running")]
        names = {e.fp[:16]: e for e in entries}

        def title_for(src):
            e = names.get(src["source"])
            return (f"Entry {src['source'][:8]}" + (f" · {human_duration(e.duration_ms)}" if e else ""))

        now = s["t"]
        idx = 0
        if not running:
            msg = card()
            l = QVBoxLayout(msg)
            l.setContentsMargins(18, 16, 18, 16)
            l.addWidget(label("No extraction running", "h2"))
            l.addWidget(label("Thumbnails are generated while a title plays or is scrubbed. Nothing is pending.", muted=True, wrap=True))
            self.col.insertWidget(idx, msg)
            idx += 1
        for x in running + done_srcs[:8]:
            c = card()
            l = QVBoxLayout(c)
            l.setContentsMargins(18, 14, 18, 14)
            l.setSpacing(6)
            head = QHBoxLayout()
            head.addWidget(label(title_for(x), "h2"))
            head.addStretch()
            st = "Running" if x.get("running") else ("Failed" if x.get("error") else "Finished")
            head.addWidget(Pill(st, ACCENT if x.get("running") else (BAD if x.get("error") else GOOD)))
            if x.get("contributed"):
                head.addWidget(Pill("Guest contribution", ACCENT_2))
            if x.get("p2_paused"):
                head.addWidget(Pill("Background paused", WARN))
            l.addLayout(head)
            tot, dn = x.get("total", 0), x.get("done", 0)
            pb = QProgressBar()
            pb.setRange(0, max(1, tot))
            pb.setValue(dn)
            pb.setProperty("state", "" if x.get("running") else "good")
            l.addWidget(pb)
            ev = x.get("events", [])
            recent = [e for e in ev if e.get("at", 0) >= (ev[-1]["at"] - 60)] if ev else []
            span = (recent[-1]["at"] - recent[0]["at"]) if len(recent) > 1 else 0
            rate = f"{(len(recent) - 1) / span * 60:.1f} thumbs/min" if span > 1 else "–"
            bw = sum(e.get("bytes", 0) for e in recent)
            reqs = sum(e.get("requests", 0) for e in recent)
            bps = f"{bw / span / 1024 / 1024 * 8:.1f} Mbit/s" if span > 1 else "–"
            l.addWidget(label(f"{dn}/{tot} thumbnails · rate {rate if x.get('running') else '–'} · last {len(recent)} events: {reqs} CDN requests, {human_bytes(bw)} ({bps})"
                              f" · covered {x.get('cov_60s', 0) * 100:.0f}% at 60 s grid", muted=True, wrap=True))
            if x.get("error"):
                l.addWidget(label(f"Error: {x['error']}" + (f" ({x['upstream_error_reason']})" if x.get("upstream_error_reason") else ""), wrap=True))
            self.col.insertWidget(idx, c)
            idx += 1
        if me.get("failed_jobs"):
            c = card()
            l = QVBoxLayout(c)
            l.setContentsMargins(18, 14, 18, 14)
            l.addWidget(label("Failed jobs", "h2"))
            for j in me["failed_jobs"][:6]:
                l.addWidget(label(f"{j.get('error')} · {j.get('host') or ''}", muted=True))
            self.col.insertWidget(idx, c)


class LogsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.lines: deque[core.LogLine] = deque(maxlen=6000)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(12)
        lay.addWidget(page_header("Logs", "The server's own log file, live."))
        bar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search logs…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(240)
        bar.addWidget(self.search)
        self.levels = {}
        for lv, col in (("DEBUG", FAINT), ("INFO", MUTED), ("WARNING", WARN), ("ERROR", BAD)):
            cb = QCheckBox(lv.title())
            cb.setChecked(lv != "DEBUG")
            cb.setStyleSheet(f"QCheckBox {{ color: {col}; }}")
            cb.stateChanged.connect(self.render)
            self.levels[lv] = cb
            bar.addWidget(cb)
        self.quiet = QCheckBox("Hide app polling")
        self.quiet.setChecked(True)
        self.quiet.stateChanged.connect(self.render)
        bar.addWidget(self.quiet)
        bar.addStretch()
        self.pause = QPushButton("Pause")
        self.pause.setCheckable(True)
        copy, folder, export = QPushButton("Copy"), QPushButton("Open folder"), QPushButton("Export diagnostics…")
        for b in (self.pause, copy, folder, export):
            bar.addWidget(b)
        lay.addLayout(bar)
        self.view = QPlainTextEdit()
        self.view.setObjectName("log")
        self.view.setReadOnly(True)
        self.view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.view.setMaximumBlockCount(6000)
        lay.addWidget(self.view, 1)
        self.status = label("", "faint")
        lay.addWidget(self.status)
        self.search.textChanged.connect(self.render)
        self.pause.toggled.connect(lambda on: self.pause.setText("Resume" if on else "Pause"))
        copy.clicked.connect(self.copy)
        folder.clicked.connect(self.open_folder)
        export.clicked.connect(self.export)
        self.tail = LogTail(win.cfg)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        self.poll()

    def _ok(self, ln: core.LogLine) -> bool:
        if not self.levels.get("WARNING" if ln.level == "CRITICAL" else ln.level, self.levels["INFO"]).isChecked():
            return False
        if self.quiet.isChecked() and ('127.0.0.1:' in ln.msg and ('"GET /admin/' in ln.msg or '"GET /v1/health' in ln.msg)):
            return False
        q = self.search.text().strip().lower()
        return not q or q in ln.raw.lower()

    def poll(self):
        new = self.tail.read_new()
        for ln in new:
            self.lines.append(ln)
        self.win.feed_activity(new)
        if new and not self.pause.isChecked():
            self.append(new)
        p = self.tail.path
        self.status.setText(f"{p.name if p else 'no log file found'} · {len(self.lines):,} lines buffered" + ("  ·  paused" if self.pause.isChecked() else ""))

    def append(self, new):
        sb = self.view.verticalScrollBar()
        at_end = sb.value() >= sb.maximum() - 4
        sel = [ln for ln in new if self._ok(ln)]
        if sel:
            self.view.appendPlainText("\n".join(self._fmt(ln) for ln in sel))
        if at_end:
            sb.setValue(sb.maximum())

    @staticmethod
    def _fmt(ln):
        return f"{ln.ts[11:] if ln.ts else '        '}  {ln.level[:4]:<4}  {redact(ln.logger + ': ' + ln.msg) if False else ln.logger + ': ' + ln.msg}"

    def render(self):
        self.view.clear()
        self.view.setPlainText("\n".join(self._fmt(ln) for ln in self.lines if self._ok(ln)))
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def copy(self):
        t = self.view.textCursor().selectedText().replace(" ", "\n") or self.view.toPlainText()
        QGuiApplication.clipboard().setText(t)
        self.win.toast("Copied.")

    def open_folder(self):
        d = Path(self.win.cfg.data_dir) / "logs"
        if d.is_dir():
            os.startfile(str(d))

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export diagnostics", str(Path.home() / "Desktop" / "openseek-diagnostics.txt"), "Text (*.txt)")
        if not path:
            return
        s = self.win.last_snap or {}
        parts = ["OpenSeek diagnostics (credentials, URLs paths and ids redacted)", time.strftime("%Y-%m-%d %H:%M:%S"),
                 f"online: {s.get('online')}", f"health: {s.get('health')}"]
        me = s.get("metrics") or {}
        parts.append("config: " + redact(str(me.get("config"))))
        parts.append("sources: " + str([{k: v for k, v in x.items() if k != 'events'} for x in me.get("sources", [])])[:3000])
        parts.append("---- last 2000 log lines ----")
        parts += [redact(ln.raw) for ln in list(self.lines)[-2000:]]
        Path(path).write_text("\n".join(parts), encoding="utf-8")
        self.win.toast("Diagnostics exported (redacted).")


class SharingPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self._busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(14)
        self.h_adv = page_header("Sharing & devices", "Let someone you trust borrow your cached thumbnails.")
        self.h_simple = page_header("Devices", "The TVs and devices that can use your server.")
        hrow = QHBoxLayout()
        hrow.addWidget(self.h_simple)
        hrow.addWidget(self.h_adv)
        hrow.addStretch()
        self.b_tv = QPushButton("Connect a TV")
        self.b_tv.setObjectName("primary")
        self.b_tv.setCursor(Qt.PointingHandCursor)
        self.b_tv.clicked.connect(win.connect_tv)
        hrow.addWidget(self.b_tv)
        lay.addLayout(hrow)
        c = card()
        cl = QVBoxLayout(c)
        cl.setContentsMargins(20, 16, 20, 16)
        cl.setSpacing(10)
        r1 = QHBoxLayout()
        self.sharing = Toggle("Library sharing")
        self.index = Toggle("Title index (ids only)")
        self.contrib = Toggle("Allow guest contributions")
        r1.addWidget(self.sharing)
        r1.addSpacing(30)
        r1.addWidget(self.index)
        r1.addSpacing(30)
        r1.addWidget(self.contrib)
        r1.addStretch()
        cl.addLayout(r1)
        self.endpoint = label("", muted=True, wrap=True)
        cl.addWidget(self.endpoint)
        r2 = QHBoxLayout()
        self.b_invite = QPushButton("New invitation…")
        self.b_invite.setObjectName("primary")
        self.b_unblock = QPushButton("Clear blocked addresses")
        self.b_hosts = QPushButton("Allowed stream hosts…")
        r2.addWidget(self.b_invite)
        r2.addWidget(self.b_unblock)
        r2.addWidget(self.b_hosts)
        r2.addStretch()
        self.cinfo = label("", muted=True, wrap=True)
        cl.addWidget(self.cinfo)
        cl.addLayout(r2)
        self.adv = [c]
        lay.addWidget(c)
        lay.addWidget(label("Devices", "h2"))
        self.devices = QTreeWidget()
        self.devices.setHeaderLabels(["Device", "Role", "Contributor", "Paired", "Last seen", "Expires"])
        self.devices.header().setStretchLastSection(False)
        self.devices.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for i in (1, 2, 3, 4, 5):
            self.devices.header().setSectionResizeMode(i, QHeaderView.ResizeToContents)
        lay.addWidget(self.devices, 1)
        r3 = QHBoxLayout()
        self.b_perm = QPushButton("Allow / remove contributor…")
        self.b_revoke = QPushButton("Revoke selected device…")
        self.b_revoke.setObjectName("danger")
        r3.addWidget(self.b_perm)
        r3.addWidget(self.b_revoke)
        r3.addStretch()
        lay.addLayout(r3)
        self.adv.append(self.b_perm)
        self.l_cj = label("Guest contributions in progress", "h2")
        lay.addWidget(self.l_cj)
        self.cjobs = QTreeWidget()
        self.cjobs.setHeaderLabels(["Device", "State", "Progress"])
        self.cjobs.setMaximumHeight(90)
        self.cjobs.header().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.cjobs)
        self.b_cstop = QPushButton("Stop selected contribution")
        self.b_cstop.setObjectName("danger")
        lay.addWidget(self.b_cstop, 0, Qt.AlignLeft)
        self.l_inv = label("Recent invitations", "h2")
        lay.addWidget(self.l_inv)
        self.invites = QTreeWidget()
        self.invites.setHeaderLabels(["Label", "State", "Expires in", "Attempts left"])
        self.invites.setMaximumHeight(130)
        self.invites.header().setSectionResizeMode(0, QHeaderView.Stretch)
        lay.addWidget(self.invites)
        self.adv += [self.l_cj, self.cjobs, self.b_cstop, self.l_inv, self.invites]
        self.sharing.toggled.connect(lambda on: self._put("/admin/sharing", {"enabled": on}))
        self.index.toggled.connect(lambda on: self._put("/admin/library", {"metadata_index": on}))
        self.contrib.toggled.connect(self.toggle_contrib)
        self.b_hosts.clicked.connect(self.edit_hosts)
        self.b_perm.clicked.connect(self.toggle_perm)
        self.b_cstop.clicked.connect(self.stop_contrib)
        self.b_invite.clicked.connect(self.new_invite)
        self.b_revoke.clicked.connect(self.revoke)
        self.b_unblock.clicked.connect(lambda: (self.win.admin.call("DELETE", "/admin/public/blocks"), self.win.toast("Blocks cleared.")))

    def apply_mode(self, advanced: bool):
        """Simple: just the devices and Connect a TV. Advanced: sharing, invitations, contributor options."""
        self.h_adv.setVisible(advanced)
        self.h_simple.setVisible(not advanced)
        for w in self.adv:
            w.setVisible(advanced)

    def _put(self, path, body):
        if self._busy:
            return
        r = self.win.admin.call("PUT", path, body)
        if r is None or r.get("_error"):
            self.win.toast("Could not change the setting (server offline?).")

    def update_snapshot(self, s: Snapshot, entries):
        sh = s.get("sharing")
        self._busy = True
        try:
            for w in (self.sharing, self.index, self.contrib, self.b_invite, self.b_unblock, self.b_revoke, self.b_perm, self.b_hosts, self.b_cstop):
                w.setEnabled(bool(sh))
            ct = s.get("contrib")
            self._ct = ct
            if ct and not ct.get("_error"):
                self.contrib.setChecked(bool(ct.get("enabled")))
                den = ct.get("denied_hosts") or []
                self.cinfo.setText("Contributors may send a stream link for a title missing from your library; your server extracts its thumbnails (one at a time, "
                                   f"{ct.get('mbps')} Mbit/s cap, yields to your own playback). Allowed hosts: {', '.join(ct.get('hosts', [])) or 'none'}."
                                   + (f"  Recently refused: {', '.join(den)}." if den else "")
                                   + ("" if ct.get("effective") else "  (Off: sharing or contributions are disabled.)"))
                self.cjobs.clear()
                names = {d.get("device_id"): d.get("name") for d in ((s.get("status") or {}).get("devices") or [])}
                for j in ct.get("jobs", []):
                    it = QTreeWidgetItem([names.get(j["device_id"], j["device_id"][:8]), j["state"], f"{j['done']}/{j['total']}"])
                    it.setData(0, Qt.UserRole, j["preview_id"])
                    self.cjobs.addTopLevelItem(it)
            if sh:
                self.sharing.setChecked(bool(sh.get("enabled")))
                self.index.setChecked(bool(sh.get("metadata_index")))
                pub = sh.get("public", {})
                if pub.get("listening"):
                    self.endpoint.setText(f"Public endpoint: {pub.get('host') or '(no public host set)'} → listening on port {pub.get('port')} · "
                                          f"{pub.get('blocked', 0)} address(es) blocked · {pub.get('refused_total', 0)} request(s) rate-limited. "
                                          "Guests can only pair with an invitation and read shared thumbnails.")
                else:
                    self.endpoint.setText("The public endpoint is not started. Invitations work on your local network only.")
            else:
                self.endpoint.setText("The server is offline or the admin API is unreachable.")
        finally:
            self._busy = False
        devs = s.get("devices")
        if devs is not None:
            sel = (self.devices.currentItem().data(0, Qt.UserRole) if self.devices.currentItem() else None)
            self.devices.clear()
            lst = devs if isinstance(devs, list) else devs.get("devices", [])
            if isinstance(devs, dict) and "devices" not in devs:
                lst = (s.get("status") or {}).get("devices", [])
            for d in lst:
                it = QTreeWidgetItem([d.get("name", "?"), d.get("role", "owner"),
                                      ("Yes" if d.get("contribute") else "Reader") if d.get("role") == "guest" else "–",
                                      ago(d.get("created", 0)), ago(d.get("last_seen", 0)),
                                      time.strftime("%Y-%m-%d", time.localtime(d["expires"])) if d.get("expires") else "never"])
                it.setData(0, Qt.UserRole, d.get("device_id"))
                it.setData(1, Qt.UserRole, (d.get("role"), bool(d.get("contribute"))))
                self.devices.addTopLevelItem(it)
                if d.get("device_id") == sel:
                    self.devices.setCurrentItem(it)
        inv = s.get("invites")
        if inv is not None:
            self.invites.clear()
            for i in (inv.get("invites", []) if isinstance(inv, dict) else [])[:8]:
                exp = i.get("expires_in_s")
                self.invites.addTopLevelItem(QTreeWidgetItem([i.get("label") or "(no label)", i.get("state", "?"),
                                                              f"{int(exp // 3600)} h" if isinstance(exp, (int, float)) and exp > 0 else "–", str(i.get("attempts_left", ""))]))

    def toggle_contrib(self, on):
        if self._busy:
            return
        if on and QMessageBox.question(self, "Allow guest contributions", "Authorised guest devices will be able to send a stream link to YOUR server, which will read it "
                                       "and generate thumbnails. Only hosts on the allowed list are accepted, one job at a time, never while your own playback is struggling. "
                                       "Only allow content you and your guest have the right to process. Each guest also needs the Contributor permission.",
                                       QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel) != QMessageBox.Yes:
            self._busy = True
            self.contrib.setChecked(False)
            self._busy = False
            return
        r = self.win.admin.call("PUT", "/admin/contributions", {"enabled": on})
        if r is None or r.get("_error"):
            self.win.toast("Could not change the setting. Restart OpenSeek once to enable contributions.")

    def edit_hosts(self):
        from PySide6.QtWidgets import QInputDialog
        cur = ", ".join((getattr(self, "_ct", None) or {}).get("hosts", []))
        txt, ok = QInputDialog.getMultiLineText(self, "Allowed stream hosts", "Domain names a contributed stream may come from (comma or one per line; subdomains included):", cur.replace(", ", "\n"))
        if ok:
            hosts = [h.strip() for h in txt.replace(",", "\n").splitlines() if h.strip()]
            r = self.win.admin.call("PUT", "/admin/contributions", {"hosts": hosts})
            self.win.toast("Saved." if r and not r.get("_error") else "Could not save.")

    def toggle_perm(self):
        it = self.devices.currentItem()
        if not it:
            return
        role, now = it.data(1, Qt.UserRole)
        if role != "guest":
            QMessageBox.information(self, "Owner device", "Only guest devices have a Contributor permission.")
            return
        r = self.win.admin.call("PUT", f"/admin/devices/{it.data(0, Qt.UserRole)}/permissions", {"contribute": not now})
        self.win.toast(("Contributor permission " + ("granted." if not now else "removed (their job was stopped).")) if r and not r.get("_error") else "Could not change it.")

    def stop_contrib(self):
        it = self.cjobs.currentItem()
        if it:
            r = self.win.admin.call("DELETE", f"/admin/contributions/{it.data(0, Qt.UserRole)}")
            self.win.toast("Stopped." if r is not None and not r.get("_error") else "Could not stop it.")

    def new_invite(self):
        pub = ((self.win.last_snap or {}).get("sharing") or {}).get("public", {})
        r = self.win.admin.call("POST", "/admin/invites", {"public": bool(pub.get("listening") and pub.get("host")), "label": ""})
        if r is None or r.get("_error"):
            QMessageBox.warning(self, "No invitation", "Could not create one. Is library sharing on?")
            return
        InviteDialog(self.win, r).exec()

    def revoke(self):
        it = self.devices.currentItem()
        if not it:
            return
        did, name = it.data(0, Qt.UserRole), it.text(0)
        if QMessageBox.question(self, "Revoke device", f"Revoke “{name}”? It loses access immediately and must be paired again.",
                                QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel) == QMessageBox.Yes:
            r = self.win.admin.call("DELETE", f"/admin/devices/{did}")
            self.win.toast("Device revoked." if r is not None and not r.get("_error") else "Could not revoke.")


class InviteDialog(QDialog):
    """Shows the invitation ONCE. The secret is never stored by the app and is not in the invitation list afterwards."""

    def __init__(self, win, inv: dict):
        super().__init__(win)
        self.setWindowTitle("Invitation")
        self.setMinimumWidth(560)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(10)
        lay.addWidget(label("Invitation ready", "h2"))
        lay.addWidget(label("Valid for %d h, one use. Send it privately; it will not be shown again." % round(inv.get("expires_in_s", 86400) / 3600), muted=True, wrap=True))
        code = QLabel(inv.get("code", ""))
        code.setStyleSheet("font-family: 'Cascadia Mono', Consolas; font-size: 24px; font-weight: 600; letter-spacing: 2px; "
                           f"background: {SURFACE}; border-radius: 12px; padding: 16px; color: {ACCENT_2};")
        code.setAlignment(Qt.AlignCenter)
        code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(code)
        lay.addWidget(label(f"Server address: {inv.get('address', '')}", muted=True))
        row = QHBoxLayout()
        b1, b2, b3 = QPushButton("Copy code"), QPushButton("Copy join link"), QPushButton("Close")
        b3.setObjectName("primary")
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch()
        row.addWidget(b3)
        lay.addLayout(row)
        b1.clicked.connect(lambda: QGuiApplication.clipboard().setText(inv.get("code", "")))
        b2.clicked.connect(lambda: QGuiApplication.clipboard().setText(inv.get("join", "")))
        b3.clicked.connect(self.accept)


class SettingsPage(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        sa = QScrollArea()
        sa.setWidgetResizable(True)
        sa.setFrameShape(QFrame.NoFrame)
        sa.setStyleSheet("QScrollArea { background: transparent; border: none; } QWidget#settingsBody { background: transparent; }")
        body = QWidget()
        body.setObjectName("settingsBody")
        sa.setWidget(body)
        outer.addWidget(sa)
        lay = QVBoxLayout(body)
        lay.setContentsMargins(32, 28, 32, 24)
        lay.setSpacing(14)
        lay.addWidget(page_header("Settings"))
        app = card()
        al = QGridLayout(app)
        al.setContentsMargins(20, 16, 20, 16)
        al.setVerticalSpacing(12)
        al.addWidget(label("Application", "h2"), 0, 0, 1, 3)
        self.tray = Toggle("Minimise to the system tray when closed")
        self.tray.setChecked(win.cfg.tray)
        self.autostart = Toggle("Start with Windows")
        self.autostart.setChecked(autostart_enabled())
        al.addWidget(self.tray, 1, 0, 1, 3)
        al.addWidget(self.autostart, 2, 0, 1, 3)
        self.adv_toggle = Toggle("Show advanced options (logs, sharing and contributor controls, server settings)")
        self.adv_toggle.setChecked(win.cfg.advanced)
        al.addWidget(self.adv_toggle, 3, 0, 1, 3)
        self._al = al
        self.data = QLineEdit(win.cfg.data_dir)
        self.backend = QLineEdit(win.cfg.backend_dir)
        for r, (cap, w) in enumerate((("OpenSeek data folder (cache, database, logs)", self.data), ("OpenSeek backend folder (to start the server)", self.backend)), start=4):
            al.addWidget(label(cap, muted=True), r, 0)
            al.addWidget(w, r, 1)
            b = QPushButton("Browse…")
            b.clicked.connect(lambda _=False, w=w: self._browse(w))
            al.addWidget(b, r, 2)
        save = QPushButton("Save")
        save.setObjectName("primary")
        al.addWidget(save, 6, 2)
        lay.addWidget(app)
        self.thumbs = ThumbQualityCard(win)
        lay.addWidget(self.thumbs.box)
        self.simple_server = card()
        ssl_ = QVBoxLayout(self.simple_server)
        ssl_.setContentsMargins(20, 16, 20, 16)
        ssl_.addWidget(label("Your server", "h2"))
        self.svc_text = label("", muted=True, wrap=True)
        ssl_.addWidget(self.svc_text)
        lay.addWidget(self.simple_server)
        self.editor = ServerSettingsCard(win)
        lay.addWidget(self.editor.box)
        self.server = card()
        sl = QVBoxLayout(self.server)
        sl.setContentsMargins(20, 16, 20, 16)
        sl.addWidget(label("Server configuration (live, read-only)", "h2"))
        self.cfgtext = label("", muted=True, wrap=True)
        sl.addWidget(self.cfgtext)
        sl.addWidget(label("These are set by the server's start-up script (tools\\run_production.ps1) and apply on restart. The app never edits them behind your back.", "faint", wrap=True))
        lay.addWidget(self.server)
        net = card()
        nl = QVBoxLayout(net)
        nl.setContentsMargins(20, 16, 20, 16)
        nl.addWidget(label("Network diagnostics", "h2"))
        self.diag = label("Press Run to check the local endpoints.", muted=True, wrap=True)
        nl.addWidget(self.diag)
        run = QPushButton("Run diagnostics")
        nl.addWidget(run, 0, Qt.AlignLeft)
        self.net = net
        lay.addWidget(net)
        lay.addStretch()
        self.adv_toggle.toggled.connect(self.on_adv)
        save.clicked.connect(self.save)
        run.clicked.connect(self.run_diag)
        self.autostart.toggled.connect(lambda on: set_autostart(on))
        self.tray.toggled.connect(lambda on: (setattr(win.cfg, "tray", on), win.cfg.save()))

    def on_adv(self, on: bool):
        self.win.cfg.advanced = on
        self.win.cfg.save()
        self.win.apply_mode()

    def apply_mode(self):
        installed, adv = self.win.cfg.installed(), self.win.cfg.advanced
        self.adv_toggle.blockSignals(True)
        self.adv_toggle.setChecked(adv)
        self.adv_toggle.blockSignals(False)
        for i in range(self._al.count()):                           # the folder pickers belong to manual setups; the installer manages them
            r = self._al.getItemPosition(i)[0]
            it = self._al.itemAt(i).widget()
            if it is not None and r >= 4:
                it.setVisible(not installed)
        self.simple_server.setVisible(installed)
        self.server.setVisible(not installed)
        self.editor.box.setVisible(installed and adv)
        self.net.setVisible(adv)
        if installed and adv:
            self.editor.load()

    def _browse(self, w):
        d = QFileDialog.getExistingDirectory(self, "Choose folder", w.text() or str(Path.home()))
        if d:
            w.setText(d)

    def save(self):
        self.win.cfg.data_dir, self.win.cfg.backend_dir = self.data.text().strip(), self.backend.text().strip()
        self.win.cfg.save()
        self.win.reload_after_config()
        self.win.toast("Saved.")

    def update_snapshot(self, s, entries):
        self.thumbs.update_snapshot(s)
        info = core.install_info()
        if info:
            if time.time() - getattr(self, "_svc_t", 0) > 8:
                self._svc_t, self._svc_state = time.time(), core.service_state(info["service"]) or "not installed"
            st = self._svc_state
            addrs = ((s.get("status") or {}).get("server") or {}).get("addresses") or []
            self.svc_text.setText(f"OpenSeek runs in the background as the Windows service \"{info['service']}\" (currently {st.lower().replace('_', ' ')}) "
                                  f"and starts with Windows. Address for your TV: {addrs[0] if addrs else '(not available yet)'}, port {self.win.cfg.api_port}. "
                                  f"Your data (cache, paired devices) is kept in {info['data_dir']}.")
        cfg = dict(((s.get("metrics") or {}).get("config")) or {})
        mx = ((s.get("status") or {}).get("cache") or {}).get("max_bytes")
        if mx:
            cfg["cache_max_bytes"] = mx
        if cfg:
            keep = ("cache_max_bytes", "retention_days", "bg_mbps", "upstream_rps", "workers_per_source", "pregen", "tolerance_s", "playback_guard",
                    "multirange_prefetch", "multirange_batch", "alt_source", "max_active_sources")
            self.cfgtext.setText("  ·  ".join(f"{k}: {human_bytes(cfg[k]) if k == 'cache_max_bytes' else cfg[k]}" for k in keep if k in cfg))
        else:
            self.cfgtext.setText("Server offline.")

    def run_diag(self):
        import socket
        c = self.win.cfg
        out = []
        for name, port in (("Owner API", c.api_port), ("Admin API (loopback)", c.admin_port)):
            with socket.socket() as sk:
                sk.settimeout(1.5)
                out.append(f"{name} 127.0.0.1:{port}: {'open' if sk.connect_ex(('127.0.0.1', port)) == 0 else 'closed'}")
        sh = (self.win.last_snap or {}).get("sharing") or {}
        pub = sh.get("public", {})
        if pub.get("listening"):
            with socket.socket() as sk:
                sk.settimeout(1.5)
                out.append(f"Public listener 127.0.0.1:{pub['port']}: {'open' if sk.connect_ex(('127.0.0.1', pub['port'])) == 0 else 'closed'}")
            host = (pub.get("host") or "").split(":")[0]
            if host:
                try:
                    out.append(f"{host} resolves to {socket.gethostbyname(host)}")
                except OSError:
                    out.append(f"{host} does not resolve")
        out.append("Data folder: " + ("found" if (Path(c.data_dir) / "openseek.db").exists() else "NOT found (set it above)"))
        self.diag.setText("\n".join(out))


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            winreg.QueryValueEx(k, "OpenSeekDesktop")
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
        if on:
            exe = sys.executable if getattr(sys, "frozen", False) else None
            if exe:
                winreg.SetValueEx(k, "OpenSeekDesktop", 0, winreg.REG_SZ, f'"{exe}" --minimized')
        else:
            try:
                winreg.DeleteValue(k, "OpenSeekDesktop")
            except OSError:
                pass


# ================================================================================================ main window
class SvcBridge(QObject):
    done = Signal(bool, str)


class SvcJob(QRunnable):
    """Run a blocking service-control call off the UI thread."""

    def __init__(self, fn, bridge: SvcBridge):
        super().__init__()
        self.fn, self.bridge = fn, bridge

    def run(self):
        try:
            ok, msg = self.fn()
        except Exception as e:  # noqa: BLE001 - never let a worker thread die silently
            ok, msg = False, f"{type(e).__name__}: {e}"
        self.bridge.done.emit(ok, msg)


class ConnectTVDialog(QDialog):
    """Show the server address and a one-time pairing code, and confirm when the TV has paired."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Connect a TV")
        self.setMinimumWidth(600)
        self.expires = 0.0
        self.known: set[str] = set()
        self.pin = ""
        self.paired = False
        self._n = 0
        lay = QVBoxLayout(self)
        lay.setContentsMargins(26, 22, 26, 20)
        lay.setSpacing(14)
        lay.addWidget(page_header("Connect a TV", "Your TV and this PC must be on the same home network (Wi-Fi or cable)."))
        box = card()
        g = QGridLayout(box)
        g.setContentsMargins(20, 16, 20, 16)
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(14)
        big = "font-size: 30px; font-weight: 700; letter-spacing: 2px;"
        g.addWidget(label("1", "h2"), 0, 0)
        g.addWidget(label("On your TV, open the OpenSeek server setting in Nuvio (Settings)."), 0, 1, 1, 2)
        g.addWidget(label("2", "h2"), 1, 0)
        g.addWidget(label("Enter this server address", muted=True), 1, 1)
        self.addr = QLabel("…")
        self.addr.setStyleSheet(big)
        self.addr.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(self.addr, 2, 1)
        self.port = label("", muted=True)
        g.addWidget(self.port, 2, 2)
        self.b_copy = QPushButton("Copy address")
        g.addWidget(self.b_copy, 1, 2)
        g.addWidget(label("3", "h2"), 3, 0)
        g.addWidget(label("Enter this pairing code", muted=True), 3, 1)
        self.code = QLabel("…")
        self.code.setStyleSheet(big + " color: " + ACCENT_2 + ";")
        self.code.setTextInteractionFlags(Qt.TextSelectableByMouse)
        g.addWidget(self.code, 4, 1)
        self.count = label("", muted=True)
        g.addWidget(self.count, 4, 2)
        self.b_new = QPushButton("Get a new code")
        g.addWidget(self.b_new, 3, 2)
        lay.addWidget(box)
        self.status = label("", wrap=True)
        lay.addWidget(self.status)
        self.details = label("", "faint", wrap=True)
        self.details.hide()
        lay.addWidget(self.details)
        row = QHBoxLayout()
        self.b_details = QPushButton("Technical details")
        row.addWidget(self.b_details)
        row.addStretch()
        b_close = QPushButton("Close")
        b_close.setObjectName("primary")
        row.addWidget(b_close)
        lay.addLayout(row)
        self.b_copy.clicked.connect(lambda: (QGuiApplication.clipboard().setText(self.addr_text), self.win.toast("Address copied.")))
        self.b_new.clicked.connect(self.new_code)
        self.b_details.clicked.connect(lambda: self.details.setVisible(not self.details.isVisible()))
        b_close.clicked.connect(self.accept)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.addr_text = ""
        self._show_address()
        self.new_code()
        self.timer.start(1000)

    def _show_address(self):
        st = (self.win.last_snap or {}).get("status") or {}
        reported = (st.get("server") or {}).get("addresses") or []
        found = core.lan_addresses(reported)
        real = [ip for ip, _ in found] or [a for a in reported if not a.startswith("127.")]
        self.addr_text = real[0] if real else ""
        self.addr.setText(self.addr_text or "not found")
        self.port.setText(f"port {self.win.cfg.api_port}")
        self.others = [f"{ip} ({name})" for ip, name in found][1:]
        if not self.addr_text:
            self.status.setText("No home-network address was found. Connect this PC to your router (Wi-Fi or cable) and try again.")
            self.status.setStyleSheet(f"color: {WARN};")

    def new_code(self):
        self.paired = False
        self.status.setStyleSheet("")
        r = self.win.admin.call("POST", "/admin/pair")
        if r is None or r.get("_error"):
            self.code.setText("…")
            self.count.setText("")
            self.status.setStyleSheet(f"color: {WARN};")
            self.status.setText("OpenSeek is not running or its admin interface is unavailable. Close this window, press Start on the dashboard and try again."
                                if r is None else f"The server refused to open a pairing window ({r.get('_error')}).")
            return
        self.code.setText(r["code"])
        self.expires = time.time() + float(r.get("expires_in_s", 600))
        self.pin = r.get("spki_pin", "")
        st = self.win.admin.get("/admin/status") or {}
        self.known = {d["device_id"] for d in st.get("devices", [])}
        self.status.setText("Waiting for your TV…  The code works once and expires in 10 minutes.")
        self.details.setText(f"Server certificate fingerprint (your TV checks it automatically): {self.pin}\n"
                             + ("Other addresses of this PC: " + ", ".join(self.others) + "\n" if self.others else "")
                             + "Nothing is opened to the internet. Only devices on your home network can reach the server, and each must pair with a code like this.")
        self.b_new.setEnabled(True)

    def tick(self):
        self._n += 1
        left = int(self.expires - time.time())
        if self.paired:
            return
        if left <= 0 and self.expires:
            self.count.setText("expired")
            self.status.setText("This code has expired. Press “Get a new code”.")
            return
        if self.expires:
            self.count.setText(f"valid for {left // 60}:{left % 60:02d}")
        if self._n % 2 == 0 and self.expires:
            st = self.win.admin.get("/admin/status")
            if st and not st.get("_error"):
                new = [d for d in st.get("devices", []) if d["device_id"] not in self.known]
                if new:
                    self.paired = True
                    self.count.setText("")
                    self.status.setStyleSheet(f"color: {GOOD}; font-weight: 600;")
                    self.status.setText(f"✓ Connected: {new[0].get('name', 'your TV')}. You can close this window.")


class ThumbQualityCard:
    """Seek-thumbnail quality: a four-step slider. Applied live by the server (no restart) and kept in its database."""

    def __init__(self, win):
        self.win = win
        self._sent: str | None = None          # last preset sent and not yet confirmed by a snapshot
        self.box = card()
        g = QGridLayout(self.box)
        g.setContentsMargins(20, 16, 20, 16)
        g.setVerticalSpacing(10)
        g.addWidget(label("Thumbnail quality", "h2"), 0, 0)
        self.value = label("", muted=True)
        g.addWidget(self.value, 0, 1, 1, 3, Qt.AlignRight)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, len(core.THUMB_PRESETS) - 1)
        self.slider.setPageStep(1)
        self.slider.setTickPosition(QSlider.NoTicks)
        self.slider.setCursor(Qt.PointingHandCursor)
        g.addWidget(self.slider, 1, 0, 1, 4)
        for i, (_n, lab, _w, _h) in enumerate(core.THUMB_PRESETS):
            g.addWidget(label(lab, "faint"), 2, i, Qt.AlignLeft if i == 0 else Qt.AlignRight if i == len(core.THUMB_PRESETS) - 1 else Qt.AlignHCenter)
        g.addWidget(label("Sharper seek previews on your TV. Higher quality uses more disk space for the cache, more bandwidth to the TV and "
                          "more processing on this PC. Standard is the default. Thumbnails already made at another quality are kept and shown "
                          "until the new ones are ready; the server applies a change straight away, without a restart.", "faint", wrap=True),
                    3, 0, 1, 4)
        self._timer = QTimer(self.box)
        self._timer.setSingleShot(True)
        self._timer.setInterval(400)               # one request per settled choice, not one per step while dragging
        self._timer.timeout.connect(self._send)
        self.slider.valueChanged.connect(self._moved)
        self.slider.sliderReleased.connect(self._timer.start)
        self._busy = True
        self.slider.setValue(core.thumb_index(core.THUMB_DEFAULT))
        self._busy = False
        self._show(self.slider.value())
        self.slider.setEnabled(False)

    def _show(self, i: int) -> None:
        _n, lab, w, h = core.THUMB_PRESETS[i]
        self.value.setText(f"{lab} · {w}×{h}")

    def _moved(self, i: int) -> None:
        self._show(i)
        if not self._busy and not self.slider.isSliderDown():
            self._timer.start()

    def _send(self) -> None:
        name = core.THUMB_PRESETS[self.slider.value()][0]
        r = self.win.admin.call("PUT", "/admin/thumbnails", {"quality": name})
        if r is None or r.get("_error"):
            self._sent = None
            self.win.toast("Could not change the thumbnail quality (server offline, or restart OpenSeek once to enable it).")
            return
        self._sent = r.get("quality", name)
        self.win.toast(f"Thumbnail quality: {core.THUMB_PRESETS[core.thumb_index(self._sent)][1]}.")

    def update_snapshot(self, s) -> None:
        t = s.get("thumbs")
        ok = bool(t) and not t.get("_error")
        self.slider.setEnabled(ok)
        if not ok or self.slider.isSliderDown() or self._timer.isActive():
            return                                  # never move the slider under the person's hand
        name = t.get("quality")
        if self._sent is not None and name != self._sent:
            return                                  # an older snapshot, taken before the change landed
        self._sent = None
        i = core.thumb_index(name)
        if i != self.slider.value():
            self._busy = True
            self.slider.setValue(i)
            self._busy = False
            self._show(i)


class ServerSettingsCard:
    """Editable server settings (installed mode, advanced). Writes the server's config file, then restarts the service."""

    def __init__(self, win):
        self.win = win
        self.box = card()
        g = QGridLayout(self.box)
        g.setContentsMargins(20, 16, 20, 16)
        g.setVerticalSpacing(10)
        g.addWidget(label("Server settings", "h2"), 0, 0, 1, 3)
        g.addWidget(label("Good defaults are already set. Change these only if you know you need to; OpenSeek restarts to apply them.", "faint", wrap=True), 1, 0, 1, 3)
        self.host = QComboBox()
        self.fields: dict[str, QLineEdit] = {}
        rows = [("Listen on", self.host, "Which network the server answers on"),
                ("Server port", self._edit("port"), "The port your TV connects to"),
                ("Admin port", self._edit("admin_port"), "This PC only; never reachable from the network"),
                ("Cache size limit (GB)", self._edit("cache_gb"), "Oldest thumbnails are removed above this"),
                ("Keep thumbnails for (days)", self._edit("retention_days"), ""),
                ("Background bandwidth (Mbit/s)", self._edit("bg_mbps"), "Speed used for thumbnails while nothing is playing"),
                ("Request limit (per second, 0 = none)", self._edit("upstream_rps"), "Politeness limit towards the video host"),
                ("Workers per title", self._edit("workers_per_source"), "1 to 4"),
                ("Titles processed at once", self._edit("max_active_sources"), "1 to 8")]
        r = 2
        for cap, w, hint in rows:
            g.addWidget(label(cap, muted=True), r, 0)
            g.addWidget(w, r, 1)
            if hint:
                g.addWidget(label(hint, "faint"), r, 2)
            r += 1
        self.guard = Toggle("Pause extraction while something is playing")
        self.multi = Toggle("Faster multi-range reading (experimental)")
        g.addWidget(self.guard, r, 0, 1, 3)
        g.addWidget(self.multi, r + 1, 0, 1, 3)
        self.log = QComboBox()
        self.log.addItems(["INFO", "DEBUG", "WARNING", "ERROR"])
        g.addWidget(label("Log detail", muted=True), r + 2, 0)
        g.addWidget(self.log, r + 2, 1)
        g.addWidget(label("Remote sharing (optional, off by default)", "h2"), r + 3, 0, 1, 3)
        g.addWidget(label("Lets invited friends outside your home use your thumbnails. It needs a router port forward and a host name; read the sharing guide first. "
                          "Leave both empty to keep everything on your home network.", "faint", wrap=True), r + 4, 0, 1, 3)
        g.addWidget(label("Public port", muted=True), r + 5, 0)
        g.addWidget(self._edit("public_port"), r + 5, 1)
        g.addWidget(label("Public host name", muted=True), r + 6, 0)
        g.addWidget(self._edit("public_host"), r + 6, 1)
        g.addWidget(label("for example name.example.org:443", "faint"), r + 6, 2)
        self.msg = label("", wrap=True)
        g.addWidget(self.msg, r + 7, 0, 1, 3)
        row = QHBoxLayout()
        b_reset = QPushButton("Recommended defaults")
        self.b_save = QPushButton("Save and restart OpenSeek")
        self.b_save.setObjectName("primary")
        row.addWidget(b_reset)
        row.addStretch()
        row.addWidget(self.b_save)
        g.addLayout(row, r + 8, 0, 1, 3)
        b_reset.clicked.connect(lambda: self.fill(core.SERVER_DEFAULTS))
        self.b_save.clicked.connect(self.save)
        self.box.hide()

    def _edit(self, key: str) -> QLineEdit:
        e = QLineEdit()
        self.fields[key] = e
        return e

    def load(self):
        self.fill({**core.SERVER_DEFAULTS, **core.read_server_config()})

    def fill(self, v: dict):
        self.host.clear()
        self.host.addItem("All private networks (recommended)", "0.0.0.0")
        self.host.addItem("This PC only", "127.0.0.1")
        for ip, name in core.lan_addresses():
            self.host.addItem(f"{ip} only ({name})", ip)
        i = self.host.findData(v.get("host", "0.0.0.0"))
        self.host.setCurrentIndex(max(i, 0))
        F = self.fields
        F["port"].setText(str(v["port"]))
        F["admin_port"].setText(str(v["admin_port"]))
        F["cache_gb"].setText(f"{v['cache_max_bytes'] / core.GIB:g}")
        F["retention_days"].setText(f"{v['retention_days']:g}")
        F["bg_mbps"].setText(f"{v['bg_mbps']:g}")
        F["upstream_rps"].setText(f"{v['upstream_rps']:g}")
        F["workers_per_source"].setText(str(v["workers_per_source"]))
        F["max_active_sources"].setText(str(v["max_active_sources"]))
        F["public_port"].setText(str(v.get("public_port", 0)))
        F["public_host"].setText(v.get("public_host", ""))
        self.guard.setChecked(bool(v["playback_guard"]))
        self.multi.setChecked(bool(v["multirange_prefetch"]))
        self.log.setCurrentText(str(v.get("log_level", "INFO")))
        self.msg.setText("")

    def save(self):
        F = self.fields
        try:
            vals = {"host": self.host.currentData(), "port": F["port"].text().strip(), "admin_port": F["admin_port"].text().strip(),
                    "cache_max_bytes": int(float(F["cache_gb"].text().strip()) * core.GIB), "retention_days": F["retention_days"].text().strip(),
                    "bg_mbps": F["bg_mbps"].text().strip(), "upstream_rps": F["upstream_rps"].text().strip(),
                    "workers_per_source": F["workers_per_source"].text().strip(), "max_active_sources": F["max_active_sources"].text().strip(),
                    "playback_guard": self.guard.isChecked(), "multirange_prefetch": self.multi.isChecked(), "log_level": self.log.currentText(),
                    "public_port": F["public_port"].text().strip() or "0", "public_host": F["public_host"].text().strip()}
            if int(vals["public_port"]) and not vals["public_host"]:
                raise ValueError("enter the public host name as well, or set the public port to 0")
            if vals["public_host"] and not int(vals["public_port"]):
                raise ValueError("enter a public port as well, or clear the host name")
            core.write_server_config(vals)
        except (ValueError, OSError) as e:
            self.msg.setStyleSheet(f"color: {BAD};")
            self.msg.setText(f"Not saved: {e}")
            return
        self.msg.setStyleSheet(f"color: {GOOD};")
        self.msg.setText("Saved. Restarting OpenSeek…")
        self.win.restart_service_async("Settings applied.")


NAV = [("Dashboard", 0), ("Library", 1), ("Live activity", 2), ("Logs", 3), ("Sharing", 4), ("Settings", 5)]


class MainWindow(QMainWindow):
    def __init__(self, cfg: Config, minimized=False):
        super().__init__()
        self.cfg = cfg
        self.admin, self.lib, self.meta = Admin(cfg), Library(cfg), Metadata()
        self.last_snap: Snapshot | None = None
        self.entries: list[Entry] = []
        self.metas: dict[str, tuple[dict | None, str]] = {}
        self.pending: set[str] = set()
        self.recent: deque = deque(maxlen=30)
        self.setWindowTitle("OpenSeek")
        self.setWindowIcon(QIcon(make_icon()))
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        h = QHBoxLayout(root)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        side = QWidget()
        side.setObjectName("sidebar")
        side.setFixedWidth(200)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(14, 18, 14, 14)
        sl.setSpacing(4)
        brand = QHBoxLayout()
        ic = QLabel()
        ic.setPixmap(make_icon(64).scaled(26, 26, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        brand.addWidget(ic)
        brand.addWidget(label("OpenSeek", "brand"))
        brand.addStretch()
        sl.addLayout(brand)
        sl.addSpacing(16)
        self.navs = []
        for text, i in NAV:
            b = QPushButton(text)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, i=i: self.go(i))
            sl.addWidget(b)
            self.navs.append(b)
        sl.addStretch()
        self.srv_pill = Pill("● …", MUTED)
        sl.addWidget(self.srv_pill, 0, Qt.AlignLeft)
        h.addWidget(side)
        self.stack = QStackedWidget()
        h.addWidget(self.stack, 1)
        self.dash, self.libp, self.act = DashboardPage(self), LibraryPage(self), ActivityPage(self)
        self.logs, self.share, self.sett = LogsPage(self), SharingPage(self), SettingsPage(self)
        self.detail = DetailPage(self)
        for p in (self.dash, self.libp, self.act, self.logs, self.share, self.sett, self.detail):
            self.stack.addWidget(p)
        self.libp.open_card.connect(self.open_card)
        self.detail.back.connect(lambda: self.go(1))
        self.toast_lbl = QLabel(self)
        self.toast_lbl.setStyleSheet(f"background: {CARD_HI}; border: 1px solid {BORDER}; border-radius: 10px; padding: 9px 16px;")
        self.toast_lbl.hide()
        self.bridge = MetaBridge()
        self.bridge.ready.connect(self._meta_ready)
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(4)
        self.svc_bridge = SvcBridge()
        self.svc_bridge.done.connect(self._svc_done)
        self.poller = Poller(cfg)
        self.poller.snap.connect(self.on_snap)
        self.poller.entries.connect(self.on_entries)
        self.poller.start()
        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(QIcon(make_icon(64)), self)
            m = QMenu()
            m.addAction("Open OpenSeek", self.show_normal)
            m.addAction("Quit", self.quit_app)
            self.tray.setContextMenu(m)
            self.tray.activated.connect(lambda r: self.show_normal() if r == QSystemTrayIcon.Trigger else None)
            self.tray.show()
        self._quit = False
        self.apply_mode()
        self.go(0)
        if cfg.installed():
            QTimer.singleShot(1500, self._ensure_service)

    # ---- simple / advanced
    def apply_mode(self):
        adv = self.cfg.advanced
        self.navs[3].setVisible(adv)                                     # Logs
        self.navs[4].setText("Sharing && devices" if adv else "Devices")
        self.share.apply_mode(adv)
        self.dash.feats.setVisible(adv)
        self.sett.apply_mode()
        if not adv and self.stack.currentIndex() == 3:
            self.go(0)

    # ---- installed (Windows service) helpers
    def _svc_name(self) -> str:
        return (core.install_info() or {}).get("service", "OpenSeek")

    def _ensure_service(self):
        if core.service_state(self._svc_name()) == "STOPPED":
            self.toast(core.service_start(self._svc_name())[1])

    def restart_service_async(self, done_msg: str):
        self._svc_msg = done_msg
        self.toast("Restarting OpenSeek…", 6000)
        self.pool.start(SvcJob(lambda: core.service_restart(self._svc_name()), self.svc_bridge))

    @Slot(bool, str)
    def _svc_done(self, ok: bool, msg: str):
        if ok:
            self.cfg = Config.load()                                      # ports may have changed
            self.reload_after_config()
            self.toast(getattr(self, "_svc_msg", "") or msg)
        else:
            self.toast(msg, 8000)

    def connect_tv(self):
        if not self.last_snap or not self.last_snap.get("online"):
            QMessageBox.information(self, "Connect a TV", "OpenSeek is not running yet. Wait a few seconds or press Start on the dashboard, then try again.")
            return
        ConnectTVDialog(self).exec()

    # ---- navigation / feedback
    def go(self, i: int):
        self.stack.setCurrentIndex(i)
        for k, b in enumerate(self.navs):
            b.setChecked(k == (i if i < 6 else 1))

    def toast(self, text: str, ms: int = 2600):
        self.toast_lbl.setText(text)
        self.toast_lbl.adjustSize()
        self.toast_lbl.move((self.width() - self.toast_lbl.width()) // 2, self.height() - 70)
        self.toast_lbl.show()
        self.toast_lbl.raise_()
        QTimer.singleShot(ms, self.toast_lbl.hide)

    # ---- data flow
    def on_snap(self, s: Snapshot):
        self.last_snap = s
        if s.get("admin_auth_failed") and not getattr(self, "_auth_warned", False):
            self._auth_warned = True
            self.toast("The server refused the admin secret. Update OpenSeek Desktop to the version that matches your server, and make sure the "
                       "data folder in Settings is the server's (it contains admin.token).", 15000)
        self.srv_pill.set("● Server online" if s["online"] else "● Server offline", GOOD if s["online"] else BAD)
        for p in (self.dash, self.act, self.share, self.sett):
            p.update_snapshot(s, self.entries)

    def on_entries(self, entries: list[Entry]):
        self.entries = entries
        for e in entries:
            if e.imdb and e.imdb not in self.metas and e.imdb not in self.pending:
                self.pending.add(e.imdb)
                self.pool.start(MetaJob(self.meta, e.imdb, e.is_episode, lambda i, m, p: self.bridge.ready.emit(i, m, p)))
        self.rebuild_cards()
        if self.last_snap:
            self.dash.update_snapshot(self.last_snap, entries)

    @Slot(str, object, str)
    def _meta_ready(self, imdb, m, poster):
        self.pending.discard(imdb)
        if m is not None or True:
            self.metas[imdb] = (m, poster)
        self.rebuild_cards()

    def rebuild_cards(self):
        self.cards = build_cards(self.entries, self.metas)
        self.libp.set_cards(self.cards)
        if self.stack.currentWidget() is self.detail and self.detail.card:
            k = self.detail.card.key
            for c in self.cards:
                if c.key == k:
                    self.detail.card = c
                    break

    def open_card(self, c: Card):
        self.detail.show_card(c)
        self.go(6)

    def refresh_library_soon(self):
        self.poller._sig = None

    def reload_after_config(self):
        self.lib.cfg = self.cfg
        self.admin.cfg = self.cfg
        self.poller.cfg = self.cfg
        self.poller.lib.cfg = self.cfg
        self.poller.admin.cfg = self.cfg
        self.logs.tail = LogTail(self.cfg)
        self.refresh_library_soon()

    # ---- recent activity (derived from the server's own log lines)
    def feed_activity(self, lines):
        for ln in lines:
            t = None
            m = ln.msg
            if "pair/claim" in m and m.rstrip().endswith("200"):
                t = ("A device paired", ACCENT_2)
            elif "/v1/lookup" in m and m.rstrip().endswith("200"):
                t = ("A guest found a shared title", ACCENT_2)
            elif "preflight ok" in m:
                t = ("Started reading a title", MUTED)
            elif "stopped (" in m and "source" in m:
                t = ("Extraction finished or stopped", MUTED)
            elif "evicted" in m:
                t = ("Old cache entries were cleaned up", WARN)
            elif ln.level in ("ERROR", "CRITICAL"):
                t = (redact(m)[:90], BAD)
            elif ln.level == "WARNING":
                t = (redact(m)[:90], WARN)
            elif "started" in m and "OpenSeek" in m:
                t = ("Server started", GOOD)
            elif "guard" in m.lower() and ("pause" in m.lower() or "resum" in m.lower()):
                t = ("Playback guard: " + redact(m)[:70], WARN)
            if t:
                self.recent.appendleft((ln.ts[11:16] if ln.ts else "", t[0], t[1]))
        self.dash.set_activity(list(self.recent)[:10])

    # ---- server control
    def do_start(self):
        if self.cfg.installed():
            self.toast(core.service_start(self._svc_name())[1])
            return
        ok, msg = core.start_server(self.cfg, self._public_host())
        self.toast(msg)

    def _public_host(self) -> str:
        pub = ((self.last_snap or {}).get("sharing") or {}).get("public", {})
        return pub.get("host", "") if pub.get("listening") else self._remembered_host()

    def _remembered_host(self) -> str:
        return getattr(self.cfg, "public_host", "")

    def do_stop(self):
        if QMessageBox.question(self, "Stop OpenSeek", "Stop the OpenSeek server? Extraction and guest access stop until you start it again. "
                                "Your cache and pairings are not touched.", QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        if self.cfg.installed():
            self.toast(core.service_stop(self._svc_name())[1])
            return
        pub = ((self.last_snap or {}).get("sharing") or {}).get("public", {})
        if pub.get("listening") and pub.get("host"):
            self.cfg.public_host = pub["host"]
            self.cfg.save()
        ok, msg = core.stop_server(self.cfg)
        self.toast(msg)

    def do_restart(self):
        if QMessageBox.question(self, "Restart OpenSeek", "Restart the server? Playback keeps working; thumbnails pause for about 6 seconds.",
                                QMessageBox.Cancel | QMessageBox.Yes, QMessageBox.Cancel) != QMessageBox.Yes:
            return
        if self.cfg.installed():
            self.restart_service_async("OpenSeek restarted.")
            return
        host = self._public_host()
        if host:
            self.cfg.public_host = host
            self.cfg.save()
        ok, msg = core.stop_server(self.cfg)
        QTimer.singleShot(1500, lambda: self.toast(core.start_server(self.cfg, host)[1]))

    # ---- window lifecycle
    def show_normal(self):
        self.showNormal()
        self.activateWindow()

    def quit_app(self):
        self._quit = True
        self.close()

    def closeEvent(self, e):
        if self.cfg.tray and self.tray and not self._quit:
            e.ignore()
            self.hide()
            self.tray.showMessage("OpenSeek", "Still running in the tray. The server is not affected.", QSystemTrayIcon.Information, 2500)
            return
        self.poller.stop()
        e.accept()
        QApplication.quit()
