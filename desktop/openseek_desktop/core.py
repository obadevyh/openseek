"""Data access for OpenSeek Desktop. Everything here is READ-ONLY against the live server's files (SQLite opened with
mode=ro, JPEG files read from the cache directory) except the few explicit owner actions that go through the loopback
admin API (identify an entry, delete one entry, sharing, invitations, devices). Nothing here writes to the database
or the cache directly, and no credential is ever stored or logged."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import ssl
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import psutil

APP = "OpenSeekDesktop"
APPDATA = Path(os.environ.get("APPDATA", str(Path.home()))) / APP
APPDATA.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APPDATA / "config.json"


def admin_headers(data_dir: str) -> dict:
    """The server's per-install admin secret: OPENSEEK_ADMIN_TOKEN, else <data>/admin.token (owner-only file)."""
    tok = os.environ.get("OPENSEEK_ADMIN_TOKEN", "")
    if not tok:
        try:
            tok = (Path(data_dir) / "admin.token").read_bytes().hex()
        except OSError:
            tok = ""
    return {"X-OpenSeek-Admin": tok}


# --------------------------------------------------------------------------------------------- installed mode
# The Windows installer sets OpenSeek up as a service and writes %ProgramData%\OpenSeek\install.json (readable by the installing user):
# {"service": "OpenSeek", "data_dir": "...", "config_file": "...\\config.json", "version": "0.2.0"}. Without it (portable ZIP, manual
# setups) the app behaves exactly as before.
PROGRAMDATA = Path(os.environ.get("OPENSEEK_PROGRAMDATA") or os.environ.get("ProgramData") or r"C:\ProgramData") / "OpenSeek"
INSTALL_JSON = PROGRAMDATA / "install.json"
GIB = 1 << 30
# server settings the desktop may edit in the server's config file (the server only honours this allowlist, see backend/openseek/config.py)
SERVER_DEFAULTS = {"port": 8787, "admin_port": 8788, "host": "0.0.0.0", "cache_max_bytes": 2 * GIB, "retention_days": 30.0, "bg_mbps": 8.0,
                   "upstream_rps": 0.0, "workers_per_source": 1, "max_active_sources": 2, "playback_guard": True, "multirange_prefetch": False,
                   "public_port": 0, "public_bind": "0.0.0.0", "public_host": "", "log_level": "INFO"}
_SERVER_RULES = {"port": (int, 1024, 65535), "admin_port": (int, 1024, 65535), "cache_max_bytes": (int, 256 << 20, 4096 * GIB),
                 "retention_days": (float, 1, 3650), "bg_mbps": (float, 0.5, 10000), "upstream_rps": (float, 0, 100),
                 "workers_per_source": (int, 1, 4), "max_active_sources": (int, 1, 8), "playback_guard": (bool,), "multirange_prefetch": (bool,),
                 "public_port": (int, 0, 65535), "host": (str,), "public_bind": (str,), "public_host": (str,), "log_level": (str,)}


# thumbnail quality presets (the server's backend/openseek/quality.py is the authority; this copy only labels the slider while
# the server is offline). The setting itself lives in the server's database: changed live over the admin API, kept across restarts.
THUMB_PRESETS = [("low", "Low", 160, 90), ("standard", "Standard", 320, 180), ("high", "High", 480, 270), ("max", "Max", 640, 360)]
THUMB_DEFAULT = "standard"


def thumb_index(name) -> int:
    """Slider position of a preset name; missing or unknown names mean the default (Standard)."""
    names = [p[0] for p in THUMB_PRESETS]
    return names.index(name) if name in names else names.index(THUMB_DEFAULT)


def install_info() -> dict | None:
    try:
        j = json.loads(INSTALL_JSON.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return j if isinstance(j, dict) and j.get("service") and j.get("data_dir") else None


def is_installed() -> bool:
    return install_info() is not None


def read_server_config() -> dict:
    """The server's own config file (installed mode). Missing or unreadable means no overrides: the server's defaults apply."""
    info = install_info()
    if not info or not info.get("config_file"):
        return {}
    try:
        j = json.loads(Path(info["config_file"]).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return j if isinstance(j, dict) else {}


def validate_server_config(values: dict) -> dict:
    """Return the cleaned dict or raise ValueError with a message a person can act on."""
    import ipaddress
    out = {}
    for k, v in values.items():
        rule = _SERVER_RULES.get(k)
        if rule is None:
            raise ValueError(f"unknown setting {k}")
        typ = rule[0]
        try:
            v = (bool(v) if typ is bool else typ(v))
        except (TypeError, ValueError):
            raise ValueError(f"{k}: not a valid {typ.__name__}") from None
        if typ in (int, float) and not rule[1] <= v <= rule[2]:
            raise ValueError(f"{k} must be between {rule[1]} and {rule[2]}")
        if k in ("host", "public_bind"):
            try:
                ip = ipaddress.ip_address(v)
            except ValueError:
                raise ValueError(f"{k}: not an IP address") from None
            if not (ip.is_unspecified or ip.is_loopback or ip.is_private) or ip.is_link_local:
                raise ValueError(f"{k} must be 0.0.0.0, this PC (127.0.0.1) or a private network address")
        if k == "log_level" and v.upper() not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
        if k == "log_level":
            v = v.upper()
        if k == "public_host" and v and not re.fullmatch(r"[A-Za-z0-9.-]{1,253}:\d{2,5}", v):
            raise ValueError("public_host must look like name.example.org:443")
        out[k] = v
    merged = {**SERVER_DEFAULTS, **read_server_config(), **out}
    if merged["port"] == merged["admin_port"]:
        raise ValueError("the server port and the admin port must differ")
    if merged["public_port"] and merged["public_port"] in (merged["port"], merged["admin_port"]):
        raise ValueError("the public port must differ from the other ports")
    return out


def write_server_config(values: dict) -> None:
    """Merge validated values into the server's config file (written in place: the installer grants this file, not its folder, to the user)."""
    info = install_info()
    if not info or not info.get("config_file"):
        raise ValueError("OpenSeek is not installed as a service on this PC")
    clean = validate_server_config(values)
    current = read_server_config()
    current.update(clean)
    Path(info["config_file"]).write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")


_VIRTUAL_NIC = re.compile(r"vethernet|wsl|virtualbox|vmware|hyper-v|docker|tailscale|zerotier|loopback|bluetooth|vpn|\btap\b|\btun\b|npcap|teredo", re.I)
_LAN_NETS = None


def lan_addresses(preferred: list[str] | None = None) -> list[tuple[str, str]]:
    """Private IPv4 addresses of this PC (address, adapter name), best first: the one the server reports, then real adapters, then virtual ones."""
    import ipaddress
    import socket
    nets = [ipaddress.ip_network(n) for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    found: list[tuple[str, str, bool]] = []
    try:
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if not st or not st.isup:
                continue
            for a in addrs:
                if a.family != socket.AF_INET:
                    continue
                try:
                    ip = ipaddress.ip_address(a.address)
                except ValueError:
                    continue
                if any(ip in n for n in nets):
                    found.append((a.address, name, bool(_VIRTUAL_NIC.search(name))))
    except (psutil.Error, OSError):
        pass
    pref = list(preferred or [])
    found.sort(key=lambda t: (t[0] not in pref, t[2], t[0]))
    return [(ip, name) for ip, name, _ in found]


def _sc(*args, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["sc.exe", *args], capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def service_state(name: str) -> str | None:
    """RUNNING, STOPPED, START_PENDING, STOP_PENDING ... or None when the service does not exist."""
    try:
        r = _sc("query", name, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode == 1060:
        return None
    m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", r.stdout)
    return m.group(1) if m else None


def _svc_result(r: subprocess.CompletedProcess, ok_msg: str) -> tuple[bool, str]:
    if r.returncode == 0:
        return True, ok_msg
    if r.returncode == 5:
        return False, ("Windows did not allow this account to control the OpenSeek service. Run the OpenSeek installer again from your own account, "
                       "or start/stop it from the Windows Services app.")
    if r.returncode in (1056, 1062):                               # already running / not started
        return True, ok_msg
    return False, "Windows reported: " + (r.stdout.strip().splitlines() or [str(r.returncode)])[-1][:160]


def service_start(name: str) -> tuple[bool, str]:
    return _svc_result(_sc("start", name), "Starting OpenSeek...")


def service_stop(name: str) -> tuple[bool, str]:
    return _svc_result(_sc("stop", name), "Stopping OpenSeek...")


def service_restart(name: str, wait_s: float = 25.0) -> tuple[bool, str]:
    """Blocking (run it off the UI thread): stop, wait until stopped, start."""
    ok, msg = service_stop(name)
    if not ok:
        return ok, msg
    end = time.time() + wait_s
    while time.time() < end and service_state(name) not in ("STOPPED", None):
        time.sleep(0.4)
    return service_start(name)


# --------------------------------------------------------------------------------------------- configuration
def _guess_data_dir() -> str:
    info = install_info()
    if info:
        return info["data_dir"]
    env = os.environ.get("OPENSEEK_DATA_DIR")
    default = str(Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "OpenSeek" / "data")
    for c in (env, default, str(Path.home() / "openseek-data")):
        if c and (Path(c) / "openseek.db").exists():
            return c
    return env or default


def _guess_backend_dir() -> str:
    for c in (os.environ.get("OPENSEEK_BACKEND_DIR"), str(Path(__file__).resolve().parents[2] / "backend"),
              str(Path.home() / "openseek-backend" / "backend")):
        if c and (Path(c) / "tools" / "run_production.ps1").exists():
            return c
    return ""


@dataclass
class Config:
    data_dir: str = field(default_factory=_guess_data_dir)
    backend_dir: str = field(default_factory=_guess_backend_dir)
    api_port: int = 8787
    admin_port: int = 8788
    tray: bool = True
    public_host: str = ""
    advanced: bool = False          # show logs, sharing controls and server settings

    @classmethod
    def load(cls) -> "Config":
        c = cls()
        saved: dict = {}
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))      # utf-8-sig: Notepad/PowerShell may add a BOM
            for k, v in saved.items():
                if hasattr(c, k):
                    setattr(c, k, v)
        except (OSError, ValueError, AttributeError):
            pass
        info = install_info()
        if not info and "advanced" not in saved:
            c.advanced = True
        if info:                                                  # installed as a service: the installer owns these
            c.data_dir = info["data_dir"]
            sc = {**SERVER_DEFAULTS, **read_server_config()}
            c.api_port, c.admin_port = int(sc["port"]), int(sc["admin_port"])
            c.backend_dir = ""
        return c

    def installed(self) -> bool:
        return is_installed()

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(self.__dict__, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------------------------- admin API / server
class Admin:
    """Loopback admin API of the running server. Every call returns None when the server is not reachable."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.auth_failed = False        # the server answered but refused the admin secret (old desktop build, wrong data folder, other user)

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=f"http://127.0.0.1:{self.cfg.admin_port}", headers=admin_headers(self.cfg.data_dir), timeout=4.0)

    def call(self, method: str, path: str, json_body=None):
        try:
            with self._client() as c:
                r = c.request(method, path, json=json_body)
            if r.status_code == 204:
                return {}
            try:
                body = r.json()
            except ValueError:
                body = {}
            if r.status_code == 403 and body.get("error") == "admin_auth":
                self.auth_failed = True
                return None                 # treated like "admin unreachable"; the window explains why
            self.auth_failed = False
            return body if r.status_code < 400 else {"_error": body.get("error", str(r.status_code)), "_status": r.status_code}
        except httpx.HTTPError:
            return None

    def get(self, path):
        return self.call("GET", path)


def api_health(cfg: Config) -> dict | None:
    """The owner API (HTTPS, self-signed): is an OpenSeek answering on 8787?"""
    try:
        with httpx.Client(verify=False, timeout=2.5) as c:
            r = c.get(f"https://127.0.0.1:{cfg.api_port}/v1/health")
        j = r.json()
        return j if j.get("service") == "openseek" else None
    except (httpx.HTTPError, ValueError):
        return None


def _is_openseek_serve(p: psutil.Process) -> bool:
    try:
        cl = " ".join(p.cmdline()).lower()
    except (psutil.Error, OSError):
        return False
    return "openseek" in cl and " serve" in cl and "python" in cl


def server_processes(cfg: Config) -> list[psutil.Process]:
    """The OpenSeek serve process(es): the one listening on the API port and its launcher parent. Nothing else, ever."""
    out: list[psutil.Process] = []
    try:
        conns = [c for c in psutil.net_connections("tcp") if c.status == psutil.CONN_LISTEN and c.laddr.port == cfg.api_port and c.pid]
    except (psutil.Error, OSError):
        conns = []
    for c in conns:
        try:
            p = psutil.Process(c.pid)
        except psutil.Error:
            continue
        if _is_openseek_serve(p) and p not in out:
            out.append(p)
            par = p.parent()
            if par is not None and _is_openseek_serve(par) and par not in out:
                out.append(par)
    return out


def start_server(cfg: Config, public_host: str = "") -> tuple[bool, str]:
    """Start through the existing run_production.ps1 (same validated settings), never a second server on a busy port."""
    if api_health(cfg) is not None:
        return False, "OpenSeek is already running on port %d." % cfg.api_port
    script = Path(cfg.backend_dir) / "tools" / "run_production.ps1"
    if not script.exists():
        return False, "run_production.ps1 not found. Set the backend folder in Settings."
    cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    if public_host:
        cmd += ["-PublicHost", public_host]
    subprocess.Popen(cmd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, "Starting OpenSeek..."


def stop_server(cfg: Config) -> tuple[bool, str]:
    procs = server_processes(cfg)
    if not procs:
        return False, "No OpenSeek server process was found on port %d (nothing was touched)." % cfg.api_port
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=6)
    for p in procs:
        try:
            if p.is_running():
                p.kill()
        except psutil.Error:
            pass
    return True, "OpenSeek stopped."


# --------------------------------------------------------------------------------------------- library (read-only)
@dataclass
class Entry:
    fp: str
    duration_ms: int
    n_tiles: int
    bytes: int
    created: float
    last_used: float
    tiles: int
    imdb: str | None = None
    tmdb: int | None = None
    season: int = 0
    episode: int = 0

    @property
    def complete(self) -> bool:
        return self.n_tiles > 0 and self.tiles >= self.n_tiles

    @property
    def identified(self) -> bool:
        return bool(self.imdb or self.tmdb)

    @property
    def is_episode(self) -> bool:
        return self.identified and (self.season > 0 or self.episode > 0)


class Library:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    @property
    def db_path(self) -> Path:
        return Path(self.cfg.data_dir) / "openseek.db"

    def entries(self) -> list[Entry]:
        if not self.db_path.exists():
            return []
        con = sqlite3.connect(f"file:{self.db_path.as_posix()}?mode=ro", uri=True, timeout=3)
        try:
            tabs = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
            meta = "release_meta" in tabs
            q = ("select e.fp,e.duration_ms,e.n_tiles,e.bytes,e.created,e.last_used,"
                 "(select count(distinct k) from tiles t where t.fp=e.fp)" +
                 (",m.imdb_id,m.tmdb_id,m.season,m.episode from entries e left join release_meta m on m.fp=e.fp"
                  if meta else " from entries e"))
            out = []
            for r in con.execute(q):
                e = Entry(*r[:7])
                if meta:
                    e.imdb, e.tmdb, e.season, e.episode = r[7], r[8], r[9] or 0, r[10] or 0
                out.append(e)
            return out
        finally:
            con.close()

    def entry_dir(self, fp: str) -> Path | None:
        if not re.fullmatch(r"[0-9a-f]{64}", fp):
            return None
        base = Path(self.cfg.data_dir) / "cache" / fp[:2] / fp
        subs = [d for d in base.glob("*") if (d / "tiles").is_dir()] if base.is_dir() else []
        return subs[0] if subs else None

    def tile_files(self, fp: str) -> list[tuple[int, Path]]:
        d = self.entry_dir(fp)
        if d is None:
            return []
        out = []
        for p in (d / "tiles").glob("*.jpg"):
            if p.stem.isdigit():
                out.append((int(p.stem), p))
        return sorted(out)

    def poster_frame(self, e: Entry) -> Path | None:
        """A REAL cached frame (about a quarter of the way in) to represent a title that has no poster."""
        files = self.tile_files(e.fp)
        if not files:
            return None
        return files[len(files) // 4][1]


# --------------------------------------------------------------------------------------------- metadata (Cinemeta, no key)
class Metadata:
    """Titles, posters, years and episode names from Cinemeta (Stremio's public catalogue), cached on disk. Missing data is
    simply missing: nothing is invented."""

    def __init__(self):
        self.dir = APPDATA / "meta"
        self.dir.mkdir(exist_ok=True)

    def _file(self, key: str) -> Path:
        return self.dir / (re.sub(r"[^A-Za-z0-9_.-]", "_", key) + ".json")

    def get(self, imdb: str, hint_series: bool) -> dict | None:
        f = self._file(imdb)
        if f.exists() and time.time() - f.stat().st_mtime < 30 * 86400:
            try:
                return json.loads(f.read_text(encoding="utf-8")) or None
            except ValueError:
                pass
        for typ in (("series", "movie") if hint_series else ("movie", "series")):
            try:
                r = httpx.get(f"https://v3-cinemeta.strem.io/meta/{typ}/{imdb}.json", timeout=8)
                m = r.json().get("meta") if r.status_code == 200 else None
            except (httpx.HTTPError, ValueError):
                return None                                   # offline: try again later, cache nothing
            if m and m.get("name"):
                m["_type"] = typ
                f.write_text(json.dumps(m), encoding="utf-8")
                return m
        f.write_text("null", encoding="utf-8")
        return None

    def poster_path(self, m: dict) -> Path | None:
        url = m.get("poster")
        if not url or not url.startswith("https://"):
            return None
        f = self.dir / (re.sub(r"[^A-Za-z0-9]", "_", m.get("id", "x")) + ".jpg")
        if f.exists() and f.stat().st_size > 1000:
            return f
        try:
            r = httpx.get(url, timeout=12)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                f.write_bytes(r.content)
                return f
        except httpx.HTTPError:
            pass
        return None

    @staticmethod
    def search(query: str) -> list[dict]:
        out = []
        for typ in ("movie", "series"):
            try:
                r = httpx.get(f"https://v3-cinemeta.strem.io/catalog/{typ}/top/search={httpx.QueryParams({'q': query})['q']}.json", timeout=8)
                for m in (r.json().get("metas") or [])[:6]:
                    if m.get("id", "").startswith("tt"):
                        out.append({"id": m["id"], "name": m.get("name", "?"), "year": m.get("releaseInfo", ""), "type": typ})
            except (httpx.HTTPError, ValueError):
                pass
        return out

    @staticmethod
    def episode_name(m: dict | None, season: int, episode: int) -> str:
        for v in (m or {}).get("videos", []) or []:
            if v.get("season") == season and v.get("episode") == episode:
                return v.get("name") or v.get("title") or ""
        return ""


# --------------------------------------------------------------------------------------------- logs
LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (DEBUG|INFO|WARNING|ERROR|CRITICAL) ([\w.]+): (.*)$")


@dataclass
class LogLine:
    ts: str
    level: str
    logger: str
    msg: str
    raw: str


class LogTail:
    """Reads the newest OpenSeek serve log (the files the server already writes), incrementally."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.path: Path | None = None
        self.pos = 0

    def latest(self) -> Path | None:
        d = Path(self.cfg.data_dir) / "logs"
        files = sorted([*d.glob("serve-*.err.log"), *d.glob("OpenSeek.err.log")], key=lambda p: p.stat().st_mtime) if d.is_dir() else []
        return files[-1] if files else None

    def read_new(self, first_bytes: int = 400_000) -> list[LogLine]:
        p = self.latest()
        if p is None:
            return []
        try:
            size = p.stat().st_size
            if p != self.path:
                self.path, self.pos = p, max(0, size - first_bytes)
            if size < self.pos:
                self.pos = 0
            if size == self.pos:
                return []
            with open(p, "rb") as f:
                f.seek(self.pos)
                data = f.read(size - self.pos)
            self.pos = size
        except OSError:
            return []
        out = []
        for raw in data.decode("utf-8", "replace").splitlines():
            m = LOG_RE.match(raw)
            out.append(LogLine(m.group(1), m.group(2), m.group(3), m.group(4), raw) if m else LogLine("", "INFO", "", raw, raw))
        return out


_REDACT = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(https?://[^\s/?#]+)[^\s]*"), r"\1/[path redacted]"),
    (re.compile(r"(?i)(token|secret|code|invite|key|password)(\"?\s*[:=]\s*\"?)[A-Za-z0-9._~+/=-]{6,}"), r"\1\2[redacted]"),
    (re.compile(r"\b[0-9a-f]{32,64}\b"), "[hex]"),
    (re.compile(r"openseek://\S+"), "openseek://[redacted]"),
]


def redact(text: str) -> str:
    for rx, rep in _REDACT:
        text = rx.sub(rep, text)
    return text


# --------------------------------------------------------------------------------------------- formatting
def human_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return "?"


def human_duration(ms: int) -> str:
    s = int(ms // 1000)
    h, m = divmod(s // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s % 60:02d}s"


def ago(ts: float) -> str:
    d = time.time() - ts
    if d < 90:
        return "just now"
    for lim, div, unit in ((3600, 60, "min"), (86400, 3600, "h"), (86400 * 60, 86400, "d")):
        if d < lim:
            return f"{int(d // div)} {unit} ago"
    return time.strftime("%Y-%m-%d", time.localtime(ts))
