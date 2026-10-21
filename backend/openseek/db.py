"""Small SQLite store for non-secret metadata (SR-15).

Persisted: hashed device tokens + device names, preview_id <-> (device, fingerprint),
per-fingerprint media metadata (duration, tile pts) and timestamps. NEVER persisted: source
URLs, forwarded headers, titles, IMDb ids or any client hint, pairing codes, raw tokens.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
  created REAL NOT NULL, last_seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS previews (
  preview_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, fp TEXT NOT NULL,
  created REAL NOT NULL, last_used REAL NOT NULL);
CREATE INDEX IF NOT EXISTS previews_dev_fp ON previews(device_id, fp);
CREATE TABLE IF NOT EXISTS entries (
  fp TEXT PRIMARY KEY, duration_ms INTEGER, n_tiles INTEGER, version INTEGER NOT NULL DEFAULT 0,
  bytes INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, last_used REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tiles (
  fp TEXT NOT NULL, profile TEXT NOT NULL DEFAULT 't320x180q75g10v1', k INTEGER NOT NULL, pts_ms INTEGER NOT NULL,
  size INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY (fp, profile, k));
CREATE TABLE IF NOT EXISTS invites (
  pair_id TEXT PRIMARY KEY, label TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'guest', salt TEXT NOT NULL,
  iterations INTEGER NOT NULL, k_hex TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
  max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
  revoked REAL, device_days REAL, tag TEXT);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS release_meta (
  fp TEXT PRIMARY KEY, imdb_id TEXT, tmdb_id INTEGER, season INTEGER NOT NULL DEFAULT 0,
  episode INTEGER NOT NULL DEFAULT 0, size_bytes INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS release_meta_imdb ON release_meta(imdb_id, season, episode);
CREATE INDEX IF NOT EXISTS release_meta_tmdb ON release_meta(tmdb_id, season, episode);
CREATE TABLE IF NOT EXISTS borrowed (
  device_id TEXT NOT NULL, release_key TEXT NOT NULL, src_fp TEXT NOT NULL, preview_id TEXT NOT NULL UNIQUE,
  a REAL NOT NULL, b REAL NOT NULL, kind TEXT NOT NULL, guest_duration_ms INTEGER NOT NULL, created REAL NOT NULL,
  PRIMARY KEY (device_id, release_key, src_fp));
CREATE TABLE IF NOT EXISTS offsets (
  device_id TEXT NOT NULL, release_key TEXT NOT NULL, src_fp TEXT NOT NULL, a REAL NOT NULL, b REAL NOT NULL,
  updated REAL NOT NULL, PRIMARY KEY (device_id, release_key, src_fp));
CREATE TABLE IF NOT EXISTS rejections (
  device_id TEXT NOT NULL, release_key TEXT NOT NULL, src_fp TEXT NOT NULL, created REAL NOT NULL,
  PRIMARY KEY (device_id, release_key, src_fp));
CREATE TABLE IF NOT EXISTS thumb_sources (
  play_fp TEXT NOT NULL, thumb_fp TEXT NOT NULL, a REAL NOT NULL, b REAL NOT NULL,
  residual_ms REAL NOT NULL, kind TEXT NOT NULL, verified REAL NOT NULL,
  PRIMARY KEY (play_fp, thumb_fp));
"""


class DB:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None,
                                    timeout=30)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Additive only: devices gain a role (existing devices stay owners), an optional expiry and the invite used;
        tiles gain the quality profile they were made at (every existing tile is Standard, the only one there was)."""
        tile_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(tiles)").fetchall()}
        if tile_cols and "profile" not in tile_cols:
            # the primary key changes, so the table is rebuilt (one transaction: all or nothing)
            self.conn.executescript("""
BEGIN;
CREATE TABLE tiles_q (
  fp TEXT NOT NULL, profile TEXT NOT NULL DEFAULT 't320x180q75g10v1', k INTEGER NOT NULL, pts_ms INTEGER NOT NULL,
  size INTEGER NOT NULL, created REAL NOT NULL, PRIMARY KEY (fp, profile, k));
INSERT INTO tiles_q(fp, profile, k, pts_ms, size, created)
  SELECT fp, 't320x180q75g10v1', k, pts_ms, size, created FROM tiles;
DROP TABLE tiles;
ALTER TABLE tiles_q RENAME TO tiles;
COMMIT;
""")
        inv_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(invites)").fetchall()}
        if inv_cols and "tag" not in inv_cols:
            self.conn.execute("ALTER TABLE invites ADD COLUMN tag TEXT")
        if inv_cols and "contribute" not in inv_cols:                 # legacy invitations: read-only (new ones say so explicitly)
            self.conn.execute("ALTER TABLE invites ADD COLUMN contribute INTEGER NOT NULL DEFAULT 0")
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(devices)").fetchall()}
        for name, ddl in (("role", "TEXT NOT NULL DEFAULT 'owner'"), ("expires", "REAL"), ("invite_id", "TEXT"),
                          ("contribute", "INTEGER NOT NULL DEFAULT 0")):
            if name not in have:
                self.conn.execute(f"ALTER TABLE devices ADD COLUMN {name} {ddl}")

    def put_thumb_source(self, play_fp: str, thumb_fp: str, a: float, b: float, residual_ms: float,
                         kind: str) -> None:
        """Provenance of tiles made from a different file: playback fp, source fp, verified mapping.
        Fingerprints and numbers only (nothing identifying the title or the URL)."""
        self.x("INSERT OR REPLACE INTO thumb_sources VALUES (?,?,?,?,?,?,?)",
               (play_fp, thumb_fp, float(a), float(b), float(residual_ms), kind, time.time()))

    def get_thumb_source(self, play_fp: str) -> tuple | None:
        r = self.q("SELECT thumb_fp,a,b,residual_ms,kind FROM thumb_sources WHERE play_fp=? "
                   "ORDER BY verified DESC LIMIT 1", (play_fp,))
        return r[0] if r else None

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---------------------------------------------------------------- settings (owner switches)
    def get_setting(self, k: str) -> str | None:
        r = self.q("SELECT v FROM settings WHERE k=?", (k,))
        return r[0][0] if r else None

    def set_setting(self, k: str, v: str) -> None:
        self.x("INSERT OR REPLACE INTO settings(k,v) VALUES(?,?)", (k, v))

    # ---------------------------------------------------------------- library (cross-release reuse)
    def upsert_release_meta(self, fp: str, imdb: str | None, tmdb: int | None, season: int, episode: int,
                            size_bytes: int, replace: bool = True) -> None:
        # replace=False: first writer wins (a guest's contribution must never relabel an existing entry)
        self.x(("INSERT OR REPLACE" if replace else "INSERT OR IGNORE") + " INTO release_meta(fp,imdb_id,tmdb_id,season,episode,size_bytes,created) "
               "VALUES(?,?,?,?,?,?,?)", (fp, imdb, tmdb, season, episode, size_bytes, time.time()))

    def purge_release_meta(self) -> int:
        return self.x("DELETE FROM release_meta")

    def release_rows(self) -> list[dict]:
        """Exactly what is stored, joined with the cache entry (for the owner's dashboard / API)."""
        out = []
        for fp, imdb, tmdb, season, episode, size, created, dur, n, tiles, used in self.q(
                "SELECT m.fp, m.imdb_id, m.tmdb_id, m.season, m.episode, m.size_bytes, m.created, e.duration_ms, e.n_tiles, "
                "(SELECT COUNT(DISTINCT k) FROM tiles t WHERE t.fp=m.fp), e.last_used FROM release_meta m "
                "LEFT JOIN entries e ON e.fp=m.fp ORDER BY m.created"):
            out.append({"fp": fp[:12], "imdb_id": imdb, "tmdb_id": tmdb, "season": season, "episode": episode,
                        "size_bytes": size, "duration_ms": dur, "tiles": tiles, "n_tiles": n, "last_used": used})
        return out

    def release_candidates(self, imdb: str | None, tmdb: int | None, season: int, episode: int) -> list[tuple]:
        """(fp, size, duration_ms, n_tiles, tiles_done) of cache entries with the same id and season/episode."""
        return self.q(
            "SELECT m.fp, m.size_bytes, e.duration_ms, e.n_tiles, (SELECT COUNT(DISTINCT k) FROM tiles t WHERE t.fp=m.fp) "
            "FROM release_meta m JOIN entries e ON e.fp=m.fp WHERE m.season=? AND m.episode=? AND e.duration_ms > 0 "
            "AND ((? IS NOT NULL AND m.imdb_id=?) OR (? IS NOT NULL AND m.tmdb_id=?))",
            (season, episode, imdb, imdb, tmdb, tmdb))

    def rejected_sources(self, device_id: str, release_key: str) -> set[str]:
        return {r[0] for r in self.q("SELECT src_fp FROM rejections WHERE device_id=? AND release_key=?",
                                     (device_id, release_key))}

    def borrowed_for(self, device_id: str, release_key: str, src_fp: str) -> str | None:
        r = self.q("SELECT preview_id FROM borrowed WHERE device_id=? AND release_key=? AND src_fp=?",
                   (device_id, release_key, src_fp))
        return r[0][0] if r else None

    def add_borrowed(self, device_id: str, release_key: str, src_fp: str, preview_id: str, a: float, b: float,
                     kind: str, guest_duration_ms: int, max_per_device: int) -> None:
        """A per-device row pointing at someone else's cache entry; bounded so a device cannot grow the table."""
        now = time.time()
        with self._lock:
            self.x("INSERT OR IGNORE INTO borrowed VALUES(?,?,?,?,?,?,?,?,?)",
                   (device_id, release_key, src_fp, preview_id, a, b, kind, guest_duration_ms, now))
            self.x("INSERT OR IGNORE INTO previews(preview_id,device_id,fp,created,last_used) VALUES(?,?,?,?,?)",
                   (preview_id, device_id, src_fp, now, now))
            extra = self.q("SELECT preview_id FROM borrowed WHERE device_id=? ORDER BY created DESC LIMIT -1 OFFSET ?",
                           (device_id, max_per_device))
            for (pid,) in extra:
                self.x("DELETE FROM borrowed WHERE preview_id=?", (pid,))
                self.x("DELETE FROM previews WHERE preview_id=?", (pid,))

    def update_borrowed_mapping(self, preview_id: str, a: float, b: float, kind: str) -> None:
        self.x("UPDATE borrowed SET a=?, b=?, kind=? WHERE preview_id=?", (a, b, kind, preview_id))

    def borrowed(self, preview_id: str) -> tuple | None:
        """(device_id, release_key, src_fp, a, b, kind, guest_duration_ms) of a borrowed preview."""
        r = self.q("SELECT device_id, release_key, src_fp, a, b, kind, guest_duration_ms FROM borrowed WHERE preview_id=?",
                   (preview_id,))
        return r[0] if r else None

    def get_offset(self, device_id: str, release_key: str, src_fp: str) -> tuple[float, float] | None:
        r = self.q("SELECT a, b FROM offsets WHERE device_id=? AND release_key=? AND src_fp=?",
                   (device_id, release_key, src_fp))
        return (r[0][0], r[0][1]) if r else None

    def put_offset(self, device_id: str, release_key: str, src_fp: str, a: float, b: float) -> None:
        self.x("INSERT OR REPLACE INTO offsets VALUES(?,?,?,?,?,?)", (device_id, release_key, src_fp, a, b, time.time()))

    def delete_offset(self, device_id: str, release_key: str, src_fp: str) -> None:
        self.x("DELETE FROM offsets WHERE device_id=? AND release_key=? AND src_fp=?", (device_id, release_key, src_fp))

    def reject_borrowed(self, device_id: str, release_key: str, src_fp: str, preview_id: str) -> None:
        with self._lock:
            self.x("INSERT OR REPLACE INTO rejections VALUES(?,?,?,?)", (device_id, release_key, src_fp, time.time()))
            self.x("DELETE FROM borrowed WHERE preview_id=?", (preview_id,))
            self.x("DELETE FROM previews WHERE preview_id=?", (preview_id,))
            self.x("DELETE FROM offsets WHERE device_id=? AND release_key=? AND src_fp=?", (device_id, release_key, src_fp))

    def q(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self.conn.execute(sql, args).rowcount

    def tx(self, stmts: list[tuple[str, tuple]]) -> None:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                for sql, args in stmts:
                    self.conn.execute(sql, args)
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- devices
    def add_device(self, device_id: str, name: str, token_hash: str, role: str = "owner",
                   expires: float | None = None, invite_id: str | None = None) -> None:
        now = time.time()
        self.x("INSERT INTO devices(device_id,name,token_hash,created,last_seen,role,expires,invite_id) "
               "VALUES(?,?,?,?,?,?,?,?)", (device_id, name, token_hash, now, now, role, expires, invite_id))

    def devices(self) -> list[dict]:
        return [{"device_id": r[0], "name": r[1], "created": r[2], "last_seen": r[3], "role": r[4], "expires": r[5],
                 "contribute": bool(r[6])}
                for r in self.q("SELECT device_id,name,created,last_seen,role,expires,contribute FROM devices ORDER BY created")]

    def set_contribute(self, device_id: str, on: bool) -> bool:
        """Only a GUEST can be a Contributor (owners have the normal API)."""
        return self.x("UPDATE devices SET contribute=? WHERE device_id=? AND role='guest'", (1 if on else 0, device_id)) > 0

    def can_contribute(self, device_id: str) -> bool:
        r = self.q("SELECT contribute FROM devices WHERE device_id=? AND role='guest'", (device_id,))
        return bool(r and r[0][0])

    def token_hashes(self) -> list[tuple[str, str, str]]:
        return self.q("SELECT token_hash, device_id, name FROM devices")

    def auth_rows(self) -> list[tuple[str, str, str, float | None]]:
        return self.q("SELECT token_hash, device_id, role, expires FROM devices")

    # ---------------------------------------------------------------- invites (one-time, expiring, revocable)
    def add_invite(self, pair_id: str, label: str, role: str, salt: str, iterations: int, k_hex: str, created: float,
                   expires: float, max_uses: int, device_days: float | None, tag: str | None = None,
                   contribute: bool = False) -> None:
        self.x("INSERT INTO invites(pair_id,label,role,salt,iterations,k_hex,created,expires,max_uses,device_days,tag,contribute) "
               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
               (pair_id, label, role, salt, iterations, k_hex, created, expires, max_uses, device_days, tag, 1 if contribute else 0))

    def invite_by_tag(self, tag: str, now: float) -> str | None:
        """pair_id of the newest still-open invite carrying this public tag."""
        r = self.q("SELECT pair_id FROM invites WHERE tag=? AND revoked IS NULL AND expires>? AND uses<max_uses "
                   "AND attempts<5 ORDER BY created DESC LIMIT 1", (tag, now))
        return r[0][0] if r else None

    def tag_in_use(self, tag: str, now: float) -> bool:
        return bool(self.q("SELECT 1 FROM invites WHERE tag=? AND revoked IS NULL AND expires>? AND uses<max_uses",
                           (tag, now)))

    def invite(self, pair_id: str) -> dict | None:
        r = self.q("SELECT pair_id,label,role,salt,iterations,k_hex,created,expires,max_uses,uses,attempts,revoked,device_days,"
                   "contribute FROM invites WHERE pair_id=?", (pair_id,))
        if not r:
            return None
        keys = ("pair_id", "label", "role", "salt", "iterations", "k_hex", "created", "expires", "max_uses", "uses",
                "attempts", "revoked", "device_days", "contribute")
        return dict(zip(keys, r[0]))

    def invites(self) -> list[dict]:
        keys = ("pair_id", "label", "role", "created", "expires", "max_uses", "uses", "attempts", "revoked")
        return [dict(zip(keys, r)) for r in self.q(
            "SELECT pair_id,label,role,created,expires,max_uses,uses,attempts,revoked FROM invites ORDER BY created DESC")]

    def bump_invite_attempts(self, pair_id: str) -> None:
        self.x("UPDATE invites SET attempts=attempts+1 WHERE pair_id=?", (pair_id,))

    def use_invite(self, pair_id: str, now: float, max_attempts: int) -> bool:
        """Atomically spend one use if the invite is still valid: the only way a device can join through it."""
        return self.x("UPDATE invites SET uses=uses+1 WHERE pair_id=? AND revoked IS NULL AND uses<max_uses "
                      "AND attempts<? AND expires>?", (pair_id, max_attempts, now)) == 1

    def revoke_invite(self, pair_id: str, now: float) -> bool:
        return self.x("UPDATE invites SET revoked=? WHERE pair_id=? AND revoked IS NULL", (now, pair_id)) == 1

    def prune_invites(self, older_than: float) -> int:
        return self.x("DELETE FROM invites WHERE expires<?", (older_than,))

    def remove_device(self, device_id: str) -> bool:
        with self._lock:
            n = self.x("DELETE FROM devices WHERE device_id=?", (device_id,))
            self.x("DELETE FROM previews WHERE device_id=?", (device_id,))
            for t in ("borrowed", "offsets", "rejections"):
                self.x(f"DELETE FROM {t} WHERE device_id=?", (device_id,))
            return n > 0

    def touch_device(self, device_id: str) -> None:
        self.x("UPDATE devices SET last_seen=? WHERE device_id=?", (time.time(), device_id))

    # ---------------------------------------------------------------- previews
    def preview_for(self, device_id: str, fp: str) -> str | None:
        r = self.q("SELECT preview_id FROM previews WHERE device_id=? AND fp=? ORDER BY created LIMIT 1",
                   (device_id, fp))
        return r[0][0] if r else None

    def add_preview(self, preview_id: str, device_id: str, fp: str) -> None:
        now = time.time()
        self.x("INSERT OR IGNORE INTO previews(preview_id,device_id,fp,created,last_used) VALUES(?,?,?,?,?)",
               (preview_id, device_id, fp, now, now))

    def preview(self, preview_id: str) -> tuple[str, str] | None:
        r = self.q("SELECT device_id, fp FROM previews WHERE preview_id=?", (preview_id,))
        return (r[0][0], r[0][1]) if r else None
