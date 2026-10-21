"""HTTP layer: the LAN HTTPS API (contract v1) and the loopback admin app.

`Core` wires the components; `create_api_app` / `create_admin_app` build the ASGI apps;
`serve()` runs both under uvicorn (API with the self-signed cert, admin on 127.0.0.1 only).
"""
from __future__ import annotations

import asyncio
import ipaddress
import hmac
import json
import logging
import os
import re
import socket
import time
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, ValidationError, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import API_VERSION, __version__
from .library import Library, clean_ids
from .pairing import InviteManager
from .altpipeline import AltMeta
from .altsource import Candidate
from .auth import CapSigner, DeviceAuth, admin_token, load_or_create_secret
from .cache import Cache, secure_mkdir
from .config import Settings
from .db import DB
from .fetch import RangeProxy
from .ids import is_cap, is_device_id, is_preview_id, parse_index
from .jobs import Busy, Job, JobManager, SourceForbidden
from .logutil import plain, setup_logging
from .netpolicy import DestinationPolicy, filter_forwarded_headers
from .pairing import PairingClosed, PairingFailed, PairingManager, RateLimited
from .public import PublicLimiter, classify
from . import quality as thumbq
from .contrib import ContributionPolicy, Spacing, clean_hosts
from .tls import ensure_certificate
from .vtt import LAYOUTS, render

log = logging.getLogger("openseek.app")

MAX_BODY = 16 * 1024
MAX_MS = 12 * 3600 * 1000
PUBLIC_PATHS = {"/v1/health", "/v1/pair/start", "/v1/pair/claim"}
# Headers a reverse proxy, tunnel or load balancer adds. The owner and admin APIs are direct-connection only: a request that carries any of them
# was relayed by something else, so its TCP peer address says nothing about who sent it. Refused (remote access is what the guest listener is for).
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "forwarded", "x-real-ip",
                 "cf-connecting-ip", "true-client-ip", "x-client-ip", "via")
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9.-]{1,253}|\[[0-9A-Fa-f:.]{2,45}\])(?::\d{1,5})?$")
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
]


# ===================================================================== core
class Core:
    def __init__(self, settings: Settings, policy: DestinationPolicy | None = None, engine_factory=None):
        self.settings = settings
        if os.name == "posix":
            os.umask(0o077)                       # files 0600, dirs 0700 (SR-16)
        self.data_dir = Path(settings.data_dir)
        secure_mkdir(self.data_dir)
        self.db = DB(self.data_dir / "openseek.db")
        # the owner's thumbnail preset survives restarts in the settings table; missing or unknown: Standard
        self.cache = Cache(self.data_dir, self.db, settings.cache_max_bytes, settings.retention_days,
                           quality=thumbq.parse(self.db.get_setting(thumbq.SETTING_KEY)))
        self.cert_path, self.key_path, self.spki_pin = ensure_certificate(self.data_dir)
        self.caps = CapSigner(load_or_create_secret(self.data_dir / "secret.key"))
        self.admin_token = admin_token(self.data_dir, settings.admin_token)
        self.auth = DeviceAuth(self.db)
        self.invites = InviteManager(self.db, self.spki_pin, self.auth.register)
        self.pairing = PairingManager(self.spki_pin, self.auth.register, invites=self.invites)
        self.pairing.sharing_enabled = self.sharing_enabled
        self.public_limiter = PublicLimiter()
        self.contrib_spacing = Spacing(6)
        self.contrib_denied: deque = deque(maxlen=12)       # recent refused HOST NAMES (never paths or queries)
        self.proxy = RangeProxy()
        self.policy = policy or DestinationPolicy(settings.allowed_ports, settings.allow_http_sources)
        self.jobs = JobManager(settings, self.policy, self.cache, self.db, self.proxy, engine_factory)
        self.library = Library(self.db, self.cache)
        self.jobs.library = self.library
        self.started = time.time()
        self.closing = False
        self.ffmpeg = _engine_available()

    def contributions_enabled(self) -> bool:
        return self.sharing_enabled() and self.db.get_setting("contrib_enabled") == "1"

    def set_thumb_quality(self, q: thumbq.Quality) -> None:
        self.db.set_setting(thumbq.SETTING_KEY, q.name)
        self.jobs.set_quality(q)

    def contrib_hosts(self) -> list[str]:
        raw = self.db.get_setting("contrib_hosts")
        if raw is not None:
            try:
                return clean_hosts(json.loads(raw))
            except ValueError:
                pass
        return clean_hosts(self.settings.contrib_hosts.split(","))

    def can_contribute(self, device_id: str | None) -> bool:
        return bool(device_id and self.contributions_enabled() and self.auth.role_of(device_id) == "guest"
                    and self.db.can_contribute(device_id))

    def sharing_enabled(self) -> bool:
        """The owner's kill switch for everything a guest can do. Off unless the owner turned it on."""
        return self.db.get_setting("sharing_enabled") == "1"

    def start(self) -> "Core":
        self.proxy.start()
        self.jobs.start()
        self.cache.evict(set())
        return self

    def stop(self) -> None:
        self.closing = True
        self.jobs.shutdown()
        self.proxy.stop()
        self.db.close()


def _engine_available() -> bool:
    import importlib.util

    return importlib.util.find_spec("av") is not None and importlib.util.find_spec("PIL") is not None


# ===================================================================== helpers
def err(status: int, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=headers)


def client_ip_allowed(host: str | None, allow_public: bool) -> bool:
    if allow_public:
        return True
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_loopback or ip.is_private or ip.is_link_local


# A guest may do exactly this and nothing else (fail closed: a route that is not listed is refused). Capability URLs
# (/v1/c/...) are checked separately and also bind to the device's own previews.
_ID32 = r"[0-9a-f]{32}"
GUEST_ALLOWED = (
    ("GET", re.compile(r"/v1/health")),
    ("GET", re.compile(r"/v1/pair/status")),
    ("DELETE", re.compile(r"/v1/pair")),                                  # leave: revokes only itself
    ("POST", re.compile(r"/v1/lookup")),
    ("PUT", re.compile(rf"/v1/previews/{_ID32}/offset")),
    ("DELETE", re.compile(rf"/v1/previews/{_ID32}/offset")),
    ("POST", re.compile(rf"/v1/previews/{_ID32}/reject")),
    ("DELETE", re.compile(rf"/v1/previews/{_ID32}")),                     # no-op for a borrowed preview
    ("POST", re.compile(r"/v1/contribute")),                              # allowed by role, then by switch + per-device permission
    ("GET", re.compile(rf"/v1/contribute/{_ID32}")),
    ("DELETE", re.compile(rf"/v1/contribute/{_ID32}")),
    ("POST", re.compile(rf"/v1/contribute/{_ID32}/telemetry")),           # the contributor's own playback health
)
GUEST_WHEN_SHARING_OFF = (("GET", re.compile(r"/v1/health")), ("GET", re.compile(r"/v1/pair/status")),
                          ("DELETE", re.compile(r"/v1/pair")))


def guest_route_allowed(method: str, path: str, allowed=GUEST_ALLOWED) -> bool:
    return any(m == method and p.fullmatch(path) for m, p in allowed)


async def _send_json(send, status: int, obj: dict, extra: list | None = None) -> None:
    body = json.dumps(obj).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
               (b"cache-control", b"no-store")] + SECURITY_HEADERS + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class Guard:
    """Pure ASGI middleware: client-IP policy (SR-17), body limit (SR-11), bearer auth (SR-6),
    security headers (SR-19). X-Forwarded-For and friends are never consulted."""

    def __init__(self, app, core: Core, admin: bool = False, public: bool = False):
        self.app = app
        self.core = core
        self.admin = admin
        self.public = public

    async def _deny(self, send, client, status: int, obj: dict, extra: list | None = None):
        if self.public and client[0]:
            self.core.public_limiter.strike(client[0])
        return await _send_json(send, status, obj, extra)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        client = scope.get("client") or (None, None)
        kind = None
        if self.public:
            # deny by default: exact (method, path) first, then per-client budgets; the peer address is the only identity
            lim, ip = self.core.public_limiter, client[0]
            if not ip:
                return await _send_json(send, 403, {"error": "forbidden"})
            if lim.blocked(ip):
                return await _send_json(send, 429, {"error": "rate_limited"}, [(b"retry-after", b"900")])
            kind = classify(scope["method"], scope.get("path", ""), guest_route_allowed)
            if kind is None:
                lim.strike(ip)
                return await _send_json(send, 404, {"error": "not_found"})
            if not lim.allow(ip, kind):
                return await _send_json(send, 429, {"error": "rate_limited"}, [(b"retry-after", b"60")])
            scope["openseek.public"] = True
            ph = self.core.settings.public_host
            if ph:
                scope["openseek.base"] = f"https://{ph}"
        elif self.admin:
            if not client_ip_allowed(client[0], False) or not ipaddress.ip_address(
                    (client[0] or "0.0.0.0").split("%")[0]).is_loopback:
                return await _send_json(send, 403, {"error": "forbidden"})
        elif not client_ip_allowed(client[0], self.core.settings.allow_public_clients):
            return await _send_json(send, 403, {"error": "forbidden"})

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        if not self.public and any(h in headers for h in PROXY_HEADERS):
            return await _send_json(send, 403, {"error": "proxy_refused"})
        clen = headers.get("content-length")
        if clen is not None and (not clen.isdigit() or int(clen) > MAX_BODY):
            return await _send_json(send, 413, {"error": "too_large"})
        body = b""
        more = True
        if scope["method"] in ("POST", "PUT", "PATCH", "DELETE"):
            while more:
                msg = await receive()
                if msg["type"] == "http.disconnect":
                    return
                body += msg.get("body", b"")
                more = msg.get("more_body", False)
                if len(body) > MAX_BODY:
                    return await _send_json(send, 413, {"error": "too_large"})
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        if self.admin:
            bad = self._admin_check(scope, headers)
            if bad:
                return await _send_json(send, 403, {"error": bad})
        else:
            path = scope.get("path", "")
            state = scope.setdefault("state", {})
            device = self.core.auth.authenticate(headers.get("authorization"))
            state["device_id"] = device
            if path not in PUBLIC_PATHS and not path.startswith("/v1/c/") and device is None:
                return await self._deny(send, client, 401, {"error": "unauthorized"})
            role = self.core.auth.role_of(device)
            state["role"] = role
            if self.public:
                if device is not None and role != "guest":
                    return await self._deny(send, client, 403, {"error": "forbidden_role"})     # owner tokens: LAN only
                if kind == "pair":
                    try:
                        j = json.loads(body or b"{}")
                    except ValueError:
                        j = None
                    if not isinstance(j, dict):
                        return await _send_json(send, 400, {"error": "bad_request"})
                    if path.endswith("/start"):
                        if not isinstance(j.get("invite"), str):                    # the owner's pairing window is never public
                            return await self._deny(send, client, 409, {"error": "pairing_closed"})
                    elif not (isinstance(j.get("pair_id"), str) and self.core.invites.knows(j["pair_id"])):
                        return await self._deny(send, client, 403, {"error": "pairing_failed"})
            if role == "guest" and not path.startswith("/v1/c/"):
                method = scope["method"]
                if not self.core.sharing_enabled() and not guest_route_allowed(method, path, GUEST_WHEN_SHARING_OFF):
                    return await _send_json(send, 403, {"error": "sharing_disabled"})
                if not guest_route_allowed(method, path):
                    return await _send_json(send, 403, {"error": "forbidden_role"})

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                if self.public and message.get("status") in (401, 403) and client[0]:
                    self.core.public_limiter.strike(client[0])          # wrong proof, bad capability, guessed id ...
                hs = list(message.get("headers", []))
                names = {k.lower() for k, _ in hs}
                for k, v in SECURITY_HEADERS:
                    if k not in names:
                        hs.append((k, v))
                if b"cache-control" not in names:
                    hs.append((b"cache-control", b"no-store"))
                hs = [(k, v) for k, v in hs if k.lower() != b"server"]
                message = dict(message, headers=hs)
            await send(message)

        await self.app(scope, replay, send_wrapper)

    def _admin_check(self, scope, headers) -> str | None:
        port = self.core.settings.admin_port
        host = headers.get("host", "")
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"):
            return "bad_host"                 # DNS-rebinding guard
        origin = headers.get("origin")
        if origin is not None and origin not in (f"http://127.0.0.1:{port}", f"http://localhost:{port}"):
            return "bad_origin"
        if scope["method"] == "GET" and scope.get("path") == "/":
            return None                       # the static dashboard shell: no data, no secret; its script gets the token from the URL fragment
        given = headers.get("x-openseek-admin", "")
        if not hmac.compare_digest(given.encode("utf-8", "replace"), self.core.admin_token.encode("utf-8")):
            return "admin_auth"               # every other admin call needs the per-install secret (also stops CSRF: a custom header)
        return None


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def _http(request, exc):  # noqa: ANN001
        code = {404: "not_found", 405: "method_not_allowed", 401: "unauthorized", 403: "forbidden",
                413: "too_large"}.get(exc.status_code, "bad_request" if exc.status_code < 500 else "internal")
        return err(exc.status_code, code)

    @app.exception_handler(RequestValidationError)
    async def _val(request, exc):  # noqa: ANN001 - never echo the input
        return err(400, "bad_request")

    @app.exception_handler(Exception)
    async def _any(request, exc):  # noqa: ANN001
        log.error("unhandled %s", type(exc).__name__)
        return err(500, "internal")


def base_url(request: Request) -> str | None:
    fixed = request.scope.get("openseek.base")        # the public listener never echoes a client-supplied Host
    if fixed:
        return fixed
    host = request.headers.get("host", "")
    if not _HOST_RE.match(host):
        return None
    return f"{request.url.scheme}://{host}"


# ===================================================================== models
class SourceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    url: StrictStr = Field(min_length=1, max_length=4096)
    headers: dict[StrictStr, StrictStr] | None = None
    duration_ms: StrictInt | None = Field(default=None, ge=0, le=MAX_MS)

    @field_validator("headers")
    @classmethod
    def _few_headers(cls, v):
        if v is not None and len(v) > 32:
            raise ValueError("too many headers")
        return v


class CandIn(BaseModel):
    """Candidate metadata only: no URLs, no credentials. Held in memory, never persisted."""
    model_config = ConfigDict(extra="ignore")
    cid: StrictStr = Field(min_length=1, max_length=64)
    title_key: StrictStr = Field(min_length=1, max_length=64)
    height: StrictInt = Field(ge=0, le=8192)
    duration_ms: StrictInt = Field(default=0, ge=0, le=MAX_MS)
    size: StrictInt = Field(default=0, ge=0)
    codec: StrictStr = Field(default="", max_length=32)
    container: StrictStr = Field(default="", max_length=32)
    name: StrictStr = Field(default="", max_length=240)
    cached: StrictBool = False
    resolved: StrictBool = False

    def to_candidate(self) -> Candidate:
        return Candidate(self.cid, self.title_key, self.height, self.duration_ms, self.size, self.codec,
                         self.container, self.name, self.cached, self.resolved)


class AltIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    play: CandIn
    candidates: list[CandIn] = Field(default_factory=list, max_length=24)
    telemetry: StrictBool = False          # the client will report playback health


class AltSourceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    cid: StrictStr = Field(min_length=1, max_length=64)
    source: SourceIn


class TelemetryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    buffer_ms: StrictInt | None = Field(default=None, ge=0, le=600_000)
    rebuffering: StrictBool = False
    host: StrictStr | None = Field(default=None, max_length=255)
    conns: StrictInt = Field(default=1, ge=0, le=64)


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    source: SourceIn
    alt: dict | None = None         # optional negotiation block; validated (AltIn) only when the feature is on
    hints: dict | None = None       # accepted for compatibility; never used as a key, never stored
    at_ms: StrictInt | None = Field(default=None, ge=0, le=MAX_MS)
    playhead_ms: StrictInt | None = Field(default=0, ge=0, le=MAX_MS)
    layout: Literal["tiles", "sheets", "hybrid"] = "hybrid"


class LookupIn(BaseModel):
    """What a device knows about the video it is about to play. No URL, no credentials, nothing about files."""
    model_config = ConfigDict(extra="ignore")
    imdb_id: StrictStr | None = Field(default=None, max_length=16)
    tmdb_id: StrictInt | None = Field(default=None, ge=1, le=10 ** 9)
    season: StrictInt = Field(default=0, ge=0, le=9999)
    episode: StrictInt = Field(default=0, ge=0, le=9999)
    duration_ms: StrictInt = Field(gt=0, le=MAX_MS)
    size_bytes: StrictInt | None = Field(default=None, ge=0)
    layout: Literal["tiles", "sheets", "hybrid"] = "hybrid"


class OffsetIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    offset_ms: StrictInt = Field(ge=-21_600_000, le=21_600_000)   # t_cache = a * t_guest + offset_ms


class LibraryIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    metadata_index: StrictBool
    purge: StrictBool = False


class PriorityIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    at_ms: StrictInt | None = Field(default=None, ge=0, le=MAX_MS)
    playhead_ms: StrictInt | None = Field(default=None, ge=0, le=MAX_MS)


class ClaimIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pair_id: StrictStr = Field(max_length=64)
    device_name: StrictStr = Field(max_length=256)
    client_proof: StrictStr = Field(max_length=128)


class StartIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    invite: StrictStr | None = Field(default=None, max_length=64)      # invitation id or its 4-letter tag (else the open window)


class InviteIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    label: StrictStr = Field(default="", max_length=64)
    ttl_s: StrictInt = Field(default=86400, ge=0, le=10 ** 7)
    device_days: StrictInt | None = Field(default=None, ge=1, le=3650)
    public: StrictBool = False        # embed the PUBLIC host:port (OPENSEEK_PUBLIC_HOST) instead of the LAN address
    read_only: StrictBool = False     # explicit Reader: by default an invited guest is a Contributor once the host enables contributions


class EntryTagIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    imdb_id: StrictStr | None = Field(default=None, max_length=12)
    tmdb_id: StrictInt | None = None
    season: StrictInt = Field(default=0, ge=0, le=9999)
    episode: StrictInt = Field(default=0, ge=0, le=9999)


class ContribIn(BaseModel):
    """What a Contributor hands over: the stream reference and what the title is. Nothing else is read."""
    model_config = ConfigDict(extra="ignore")
    source: SourceIn
    imdb_id: StrictStr | None = Field(default=None, max_length=16)
    tmdb_id: StrictInt | None = Field(default=None, ge=1, le=10 ** 9)
    season: StrictInt = Field(default=0, ge=0, le=9999)
    episode: StrictInt = Field(default=0, ge=0, le=9999)
    duration_ms: StrictInt = Field(ge=60_000, le=MAX_MS)


class ContribAdminIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: StrictBool | None = None
    hosts: list[StrictStr] | None = Field(default=None, max_length=64)


class PermIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    contribute: StrictBool


class SharingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    enabled: StrictBool


class ThumbQualityIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    quality: Literal["low", "standard", "high", "max"]


# ===================================================================== API app
def create_api_app(core: Core, public: bool = False) -> FastAPI:
    app = FastAPI(title="OpenSeek", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    _install_error_handlers(app)
    jobs = core.jobs
    s = core.settings

    def preview_body(request: Request, job: Job, as_id: str) -> dict:
        snap = jobs.snapshot(job)
        base = base_url(request)
        exp = core.caps.current_exp()
        cap = core.caps.cap(job.device_id, as_id, exp)
        vtt_url = f"{base}/v1/c/{cap}/{as_id}/thumbs.vtt?exp={exp}&layout={job.layout}" if base else None
        state = snap["state"]
        body = {"preview_id": as_id, "state": state, "error": snap["error"], "vtt_url": vtt_url,
                "vtt_version": snap["vtt_version"], "source_duration_ms": snap["source_duration_ms"],
                "tile_interval_ms": 10_000, "tolerance_ms": s.tolerance_ms,
                "progress": {"tiles_done": snap["tiles_done"], "tiles_total": snap["tiles_total"]},
                "refresh_after_ms": {"preparing": 1000, "ready": 2000}.get(state, 10_000)}
        alt_view = jobs.alt_view(job)
        if alt_view is not None:                        # key absent for clients/servers without the feature
            body["alt"] = alt_view
        return body

    def device(request: Request) -> str:
        return request.state.device_id

    def job_or_404(request: Request, preview_id: str) -> Job | None:
        if not is_preview_id(preview_id):
            return None
        return jobs.get(device(request), preview_id)

    @app.get("/v1/health")
    async def health(request: Request):
        if request.scope.get("openseek.public"):
            return {"service": "openseek", "api": API_VERSION}       # nothing about the host, version, tools or pairing state
        return {"service": "openseek", "api": API_VERSION, "version": __version__, "ffmpeg": core.ffmpeg,
                "pairing_open": core.pairing.is_open()}

    @app.post("/v1/pair/start")
    async def pair_start(body: StartIn | None = None):
        try:
            return core.pairing.start(body.invite if body is not None else None)
        except RateLimited:
            return err(429, "rate_limited", {"Retry-After": "60"})
        except PairingClosed:
            return err(409, "pairing_closed")

    @app.post("/v1/pair/claim")
    async def pair_claim(body: ClaimIn):
        try:
            return core.pairing.claim(body.pair_id, body.device_name, body.client_proof)
        except RateLimited:
            return err(429, "rate_limited", {"Retry-After": "60"})
        except PairingFailed:
            return err(403, "pairing_failed")

    @app.get("/v1/pair/status")
    async def pair_status(request: Request):
        d = device(request)
        name = next((x["name"] for x in core.db.devices() if x["device_id"] == d), None)
        core.db.touch_device(d)
        return {"device_id": d, "device_name": name, "contribute": core.can_contribute(d)}

    @app.delete("/v1/pair", status_code=204)
    async def pair_revoke(request: Request):
        d = device(request)
        await asyncio.to_thread(jobs.revoke_device, d)
        core.auth.revoke(d)
        return Response(status_code=204)

    @app.post("/v1/previews", status_code=202)
    async def create_preview(request: Request, body: PreviewIn):
        if request.url.query:
            return err(400, "bad_request")          # SR-4: URLs never travel in query strings
        if core.closing:
            return err(503, "unavailable")
        if base_url(request) is None:
            return err(400, "bad_request")
        headers = filter_forwarded_headers(body.source.headers)
        try:
            alt = None
            if body.alt is not None and s.alt_source:
                try:
                    ai = AltIn.model_validate(body.alt)
                except ValidationError:
                    ai = None                           # a malformed block never breaks the preview request
                if ai is not None and ai.telemetry:     # no telemetry => never offered
                    skip = [x.strip().lower() for x in s.alt_skip_names.split(",") if x.strip()]
                    cands = [c.to_candidate() for c in ai.candidates if not any(k in c.name.lower() for k in skip)]
                    alt = AltMeta(ai.play.to_candidate(), cands, True)
            hints = None
            if isinstance(body.hints, dict):                 # only these four ids are ever looked at (and only with opt-in)
                hints = {k: body.hints.get(k) for k in ("imdb_id", "tmdb_id", "season", "episode")}
            job = await asyncio.to_thread(jobs.create, device(request), body.source.url, headers, body.at_ms,
                                          body.playhead_ms, body.layout, alt, hints)
        except SourceForbidden:
            return err(400, "source_forbidden")
        except Busy:
            return err(429, "busy", {"Retry-After": "30"})
        return JSONResponse(preview_body(request, job, job.preview_id), status_code=202)

    @app.get("/v1/previews/{preview_id}")
    async def get_preview(request: Request, preview_id: str, wait_version: int | None = None,
                          wait_ms: int | None = None):
        job = job_or_404(request, preview_id)
        if job is None:
            return err(404, "not_found")
        jobs.touch(job)
        if wait_version is not None:
            deadline = time.monotonic() + max(0, min(int(wait_ms if wait_ms is not None else 1000), 2000)) / 1000
            while jobs.version(job) <= wait_version and time.monotonic() < deadline:
                if jobs.state(job)[0] in ("failed", "complete") and jobs.version(job) <= wait_version:
                    break
                await asyncio.sleep(0.025)
        return preview_body(request, job, preview_id)

    @app.post("/v1/previews/{preview_id}/priority")
    async def set_priority(request: Request, preview_id: str, body: PriorityIn):
        job = job_or_404(request, preview_id)
        if job is None:
            return err(404, "not_found")
        jobs.priority(job, body.at_ms, body.playhead_ms)
        return {"vtt_version": jobs.version(job), "state": jobs.state(job)[0]}

    @app.post("/v1/previews/{preview_id}/alt-source", status_code=202)
    async def alt_source(request: Request, preview_id: str, body: AltSourceIn):
        if request.url.query:
            return err(400, "bad_request")
        job = job_or_404(request, preview_id)
        if job is None:
            return err(404, "not_found")
        headers = filter_forwarded_headers(body.source.headers)
        ok = await asyncio.to_thread(jobs.alt_attach, job, body.cid, body.source.url, headers)
        if not ok:
            return err(409, "not_wanted")
        return {"state": "validating"}

    @app.post("/v1/previews/{preview_id}/telemetry", status_code=204)
    async def telemetry(request: Request, preview_id: str, body: TelemetryIn):
        job = job_or_404(request, preview_id)
        if job is None:
            return err(404, "not_found")
        jobs.telemetry(job, body.buffer_ms, body.rebuffering, body.host, body.conns)
        return Response(status_code=204)

    @app.post("/v1/lookup")
    async def lookup(request: Request, body: LookupIn):
        """Reuse of a cached timeline by a different release of the same title. A pure read: no job, no source, no
        network. Every candidate is approximate; the device can adjust or reject it."""
        if not core.library.enabled():
            return err(403, "library_disabled")
        ids = clean_ids(body.imdb_id, body.tmdb_id, body.season, body.episode)
        if ids is None:
            return err(400, "bad_request")
        base = base_url(request)
        if base is None:
            return err(400, "bad_request")
        res = await asyncio.to_thread(core.library.lookup, device(request), ids, body.duration_ms, body.size_bytes)
        if not res["candidates"]:
            return err(404, "not_cached")
        exp = core.caps.current_exp()
        for c in res["candidates"]:
            cap = core.caps.cap(device(request), c["preview_id"], exp)
            c["vtt_url"] = f"{base}/v1/c/{cap}/{c['preview_id']}/thumbs.vtt?exp={exp}&layout={body.layout}"
        return res

    # ---------------------------------------------------------------- guest contributions (narrow, off by default)
    @app.post("/v1/contribute", status_code=202)
    async def contribute(request: Request, body: ContribIn):
        """An authorised Contributor hands over its stream so thumbnails are generated once for everybody. Reuses a cached
        compatible release first; otherwise starts ONE background extraction under the contribution policy."""
        d = device(request)
        if request.url.query or core.closing:
            return err(400, "bad_request")
        if not core.can_contribute(d):
            return err(403, "contribution_not_allowed")
        if not core.contrib_spacing.hit(d):
            return err(429, "rate_limited", {"Retry-After": "60"})
        ids = clean_ids(body.imdb_id, body.tmdb_id, body.season, body.episode)
        base = base_url(request)
        if ids is None or base is None:
            return err(400, "ids_required")
        if core.library.enabled():                       # 1. a compatible release may already be cached: reuse, no extraction
            res = await asyncio.to_thread(core.library.lookup, d, ids, body.duration_ms, None)
            if res["candidates"]:
                exp = core.caps.current_exp()
                for c in res["candidates"]:
                    cap = core.caps.cap(d, c["preview_id"], exp)
                    c["vtt_url"] = f"{base}/v1/c/{cap}/{c['preview_id']}/thumbs.vtt?exp={exp}&layout=hybrid"
                return JSONResponse({"state": "reused", **res}, status_code=200)
        policy = ContributionPolicy(core.policy, core.contrib_hosts(), s.contrib_https_only,
                                    on_denied=lambda h: core.contrib_denied.append((time.time(), h)))
        hints = {"imdb_id": ids.imdb, "tmdb_id": ids.tmdb, "season": ids.season, "episode": ids.episode}
        try:
            job = await asyncio.to_thread(jobs.create, d, body.source.url, filter_forwarded_headers(body.source.headers), None, 0,
                                          "hybrid", None, hints, policy, True)
        except SourceForbidden:
            return err(403, "source_not_allowed")                 # unsafe, private, redirected off-list: never says which
        except Busy:
            return err(429, "busy", {"Retry-After": "60"})
        return JSONResponse(preview_body(request, job, job.preview_id), status_code=202)

    def _my_contribution(request: Request, preview_id: str):
        if not is_preview_id(preview_id):
            return None
        job = jobs.get(device(request), preview_id)
        return job if job is not None and job.contributed else None

    @app.get("/v1/contribute/{preview_id}")
    async def contribution_status(request: Request, preview_id: str):
        job = _my_contribution(request, preview_id)
        if job is None or not core.can_contribute(device(request)):
            return err(404, "not_found")
        jobs.touch(job)
        await asyncio.to_thread(jobs.resume, job)         # continues after a pause or an owner preemption; cached tiles are kept
        return preview_body(request, job, preview_id)

    @app.post("/v1/contribute/{preview_id}/telemetry", status_code=204)
    async def contribution_telemetry(request: Request, preview_id: str, body: TelemetryIn):
        job = _my_contribution(request, preview_id)
        if job is None or not core.can_contribute(device(request)):
            return err(404, "not_found")
        jobs.telemetry(job, body.buffer_ms, body.rebuffering, body.host, body.conns)     # the viewer's playback guards HER job
        return Response(status_code=204)

    @app.delete("/v1/contribute/{preview_id}", status_code=204)
    async def contribution_stop(request: Request, preview_id: str):
        job = _my_contribution(request, preview_id)
        if job is None:
            return err(404, "not_found")
        await asyncio.to_thread(jobs.delete, job)
        return Response(status_code=204)

    @app.put("/v1/previews/{preview_id}/offset")
    async def set_offset(request: Request, preview_id: str, body: OffsetIn):
        if not is_preview_id(preview_id):
            return err(404, "not_found")
        r = await asyncio.to_thread(core.library.set_offset, device(request), preview_id, body.offset_ms)
        return r if r is not None else err(404, "not_found")

    @app.delete("/v1/previews/{preview_id}/offset", status_code=204)
    async def clear_offset(request: Request, preview_id: str):
        if not is_preview_id(preview_id) or not await asyncio.to_thread(core.library.clear_offset, device(request), preview_id):
            return err(404, "not_found")
        return Response(status_code=204)

    @app.post("/v1/previews/{preview_id}/reject", status_code=204)
    async def reject_preview(request: Request, preview_id: str):
        if not is_preview_id(preview_id) or not await asyncio.to_thread(core.library.reject, device(request), preview_id):
            return err(404, "not_found")
        return Response(status_code=204)

    @app.delete("/v1/previews/{preview_id}", status_code=204)
    async def delete_preview(request: Request, preview_id: str):
        job = job_or_404(request, preview_id)
        if job is None:
            b = core.db.borrowed(preview_id) if is_preview_id(preview_id) else None
            if b is not None and b[0] == device(request):
                return Response(status_code=204)             # a borrowed preview has no job to stop: nothing to do
            return err(404, "not_found")
        await asyncio.to_thread(jobs.delete, job)
        return Response(status_code=204)

    # ---------------------------------------------------------------- data plane
    def url_quality(request: Request) -> thumbq.Quality | None:
        """The quality an image URL names (`q=`; absent = Standard, the original URLs). None: unknown."""
        name = request.query_params.get("q")
        return thumbq.STANDARD if name is None else thumbq.BY_NAME.get(name)

    def data_access(request: Request, cap: str, preview_id: str):
        """Returns (device_id, fp, job) when the capability (or Bearer) grants access, else None."""
        if not is_preview_id(preview_id):
            return None
        found = jobs.lookup_data(preview_id)
        if found is None:
            return None
        dev, fp, job = found
        if core.auth.role_of(dev) is None:
            return None                                   # revoked or expired device: its URLs stop working at once
        if request.scope.get("openseek.public") and core.auth.role_of(dev) != "guest":
            return None                                   # an owner's own previews are never served to the internet
        if core.auth.role_of(dev) == "guest" and not core.sharing_enabled():
            return None                                   # kill switch
        bearer = request.state.device_id
        if bearer is not None and bearer == dev:
            return found
        exp_s = request.query_params.get("exp", "")
        if not is_cap(cap) or not exp_s.isdigit() or len(exp_s) > 12:
            return None
        if not core.caps.verify(cap, dev, preview_id, int(exp_s)):
            return None
        return found

    @app.get("/v1/c/{cap}/{preview_id}/thumbs.vtt")
    async def vtt(request: Request, cap: str, preview_id: str):
        acc = data_access(request, cap, preview_id)
        if acc is None:
            return err(403, "forbidden")
        dev, fp, job = acc
        layout = request.query_params.get("layout") or (job.layout if job else "hybrid")
        if layout not in LAYOUTS:
            return err(400, "bad_request")
        base = base_url(request)
        if base is None:
            return err(400, "bad_request")
        if job is not None:
            jobs.touch(job)
        q = core.jobs.quality
        e = core.cache.entry(fp, q) if fp else None
        exp = core.caps.current_exp()
        m = core.library.mapping_for(preview_id) if job is None else None      # a borrowed preview: remap onto the guest's timeline
        if e is not None:
            with e.lock:
                tiles = dict(e.tiles)
        else:
            tiles = {}
        fallback = await asyncio.to_thread(core.jobs.fallback_tiles, fp) if e is not None else []
        text = render(base, core.caps.cap(dev, preview_id, exp), preview_id, exp, layout,
                      tiles, e.duration_ms if e else None, e.n_tiles if e else None,
                      s.tolerance_ms, (m[0], m[1], m[2]) if m is not None else None, q, fallback)
        return Response(text, media_type="text/vtt; charset=utf-8", headers={"Cache-Control": "no-store"})

    @app.get("/v1/c/{cap}/{preview_id}/tiles/{name}")
    async def tile(request: Request, cap: str, preview_id: str, name: str):
        acc = data_access(request, cap, preview_id)
        if acc is None:
            return err(403, "forbidden")
        dev, fp, job = acc
        k = parse_index(name[:-4]) if name.endswith(".jpg") else None
        q = url_quality(request)
        if k is None or fp is None or q is None:
            return err(404, "not_found")
        data = await asyncio.to_thread(core.cache.tile_bytes, fp, k, q)
        if data is None:
            return err(404, "not_found")
        if job is not None:
            jobs.touch(job)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/v1/c/{cap}/{preview_id}/sheets/{name}")
    async def sheet(request: Request, cap: str, preview_id: str, name: str):
        acc = data_access(request, cap, preview_id)
        if acc is None:
            return err(403, "forbidden")
        dev, fp, job = acc
        n = parse_index(name[:-4], 99_999) if name.endswith(".jpg") else None
        q = url_quality(request)
        if n is None or fp is None or q is None:
            return err(404, "not_found")
        got = await asyncio.to_thread(core.cache.sheet_bytes, fp, n, q)
        if got is None:
            return err(404, "not_found")
        if job is not None:
            jobs.touch(job)
        return Response(got[0], media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    return Guard(app, core, public=public)  # type: ignore[return-value]


def api_openapi(core: Core) -> dict:
    """OpenAPI document of the API app (the live app does not serve it)."""
    guard = create_api_app(core)
    app: FastAPI = guard.app
    return app.openapi()


# ===================================================================== admin app
_ADMIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>OpenSeek admin</title>
<style>body{font:15px system-ui,sans-serif;margin:2em;max-width:52em;color:#222}code{font-size:1.1em}
table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:.3em .6em}.code{font-size:2.4em;
letter-spacing:.15em;font-family:monospace}button{font-size:1em;margin:.2em}</style></head><body>
<h1>OpenSeek</h1><div id=s>loading…</div>
<script>
const T=(location.hash||'').slice(1)||sessionStorage.getItem('osk')||'';if(T)sessionStorage.setItem('osk',T);history.replaceState(null,'',location.pathname);
const H={'X-OpenSeek-Admin':T};
function esc(t){return String(t).replace(/[&<>"']/g,c=>'&#'+c.charCodeAt(0)+';')}
async function act(m,u){await fetch(u,{method:m,headers:H});load()}
async function sharing(on){await fetch('/admin/sharing',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({enabled:on})});load()}
async function meta(on){await fetch('/admin/library',{method:'PUT',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({metadata_index:on})});load()}
async function mkinvite(){const r=await fetch('/admin/invites',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},H),body:JSON.stringify({public:!!window.pubOn})});
const j=await r.json();window.lastInvite=r.ok?('Server '+j.address+' - code '+j.code+' (valid '+Math.round(j.expires_in_s/3600)+' h, one use). Join string: '+j.join):'Could not create: '+j.error;load()}
async function load(){if(!T){document.getElementById('s').textContent='Open this page with: python -m openseek dashboard';return}
const r=await (await fetch('/admin/status',{headers:H})).json();let h='';
h+='<h2>Server</h2><p>API port <code>'+esc(r.server.port)+'</code>, addresses: <code>'+esc(r.server.addresses.join(', '))+
'</code><br>Pin: <code>'+esc(r.server.spki_pin)+'</code></p><h2>Pairing</h2>';
if(r.pairing.open){h+='<p class=code>'+esc(r.pairing.code)+'</p><p>expires in '+esc(r.pairing.expires_in_s)+
' s, attempts left '+esc(r.pairing.attempts_left)+'</p><button onclick="act(\\'DELETE\\',\\'/admin/pair\\')">Close</button>'}
else{h+=(r.pairing.burned?'<p><b>Last window burned after too many wrong codes.</b></p>':'')+
'<button onclick="act(\\'POST\\',\\'/admin/pair\\')">Open pairing window</button>'}
h+='<h2>Devices</h2><table><tr><th>id</th><th>name</th><th></th></tr>';
for(const d of r.devices){h+='<tr><td><code>'+esc(d.device_id)+'</code></td><td>'+esc(d.name)+
'</td><td><button onclick="act(\\'DELETE\\',\\'/admin/devices/'+esc(d.device_id)+'\\')">Revoke</button></td></tr>'}
h+='</table>';
const sh=await (await fetch('/admin/sharing',{headers:H})).json();
h+='<h2>Sharing</h2><p>Sharing is <b>'+(sh.enabled?'ON':'OFF')+'</b> <button onclick="sharing('+(sh.enabled?'false':'true')+')">Turn '+(sh.enabled?'off':'on')+
'</button> &nbsp; Title index (ids only) is <b>'+(sh.metadata_index?'ON':'OFF')+'</b> <button onclick="meta('+(sh.metadata_index?'false':'true')+')">Turn '+(sh.metadata_index?'off':'on')+'</button></p>';
window.pubOn=!!(sh.public&&sh.public.listening&&sh.public.host);if(sh.enabled){h+='<button onclick="mkinvite()">Create invitation'+(window.pubOn?' (public: '+esc(sh.public.host)+')':' (LAN)')+'</button> <span id=inv>'+esc(window.lastInvite||'')+'</span><table><tr><th>invitation</th><th>state</th><th>expires in</th><th></th></tr>';
for(const i of sh.invites){h+='<tr><td>'+esc(i.label||i.invite_id.slice(0,8))+'</td><td>'+esc(i.state)+'</td><td>'+esc(i.expires_in_s)+' s</td><td>'+
(i.state=='open'?'<button onclick="act(\\'DELETE\\',\\'/admin/invites/'+esc(i.invite_id)+'\\')">Revoke</button>':'')+'</td></tr>'}
h+='</table>'}
h+='<h2>Cache</h2><p>'+esc(r.cache.entries)+' titles, '+esc(r.cache.tiles)+' tiles, '+
esc((r.cache.bytes/1048576).toFixed(1))+' MiB of '+esc((r.cache.max_bytes/1048576).toFixed(0))+' MiB; '+
esc(r.active_sources)+' active source(s)</p>';document.getElementById('s').innerHTML=h}
load();setInterval(load,3000);
</script></body></html>"""


def lan_addresses() -> list[str]:
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))   # TEST-NET; UDP connect sends no packet, it only picks a route
            out.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return out or ["127.0.0.1"]


def create_admin_app(core: Core) -> FastAPI:
    app = FastAPI(title="OpenSeek admin", docs_url=None, redoc_url=None, openapi_url=None,
                  redirect_slashes=False)
    _install_error_handlers(app)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(_ADMIN_HTML, headers={
            "Content-Security-Policy": "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                       "connect-src 'self'; frame-ancestors 'none'"})

    @app.get("/admin/status")
    async def status():
        return {"server": {"port": core.settings.port, "addresses": lan_addresses(), "spki_pin": core.spki_pin},
                "pairing": core.pairing.admin_status(), "devices": core.db.devices(),
                "cache": core.cache.stats(), "active_sources": len(core.jobs.running_sources())}

    @app.get("/admin/metrics")
    async def metrics():
        # No upstream URLs, capabilities, device identities or pairing material.
        sources = []
        with core.jobs.lock:
            current = list(core.jobs.sources.values())
            failed_jobs = [{"error": j.error, "host": j.diagnostic_host}
                           for j in core.jobs.jobs.values() if j.error]
        for src in current:
            entry = core.cache.entry(src.fp)
            covered_ms = duration_ms = 0
            pts_sorted: list[int] = []
            tile_keys: set[int] = set()
            if entry is not None:
                with entry.lock:
                    duration_ms = entry.duration_ms or 0
                    pts_sorted = sorted(entry.tiles.values())
                    tile_keys = set(entry.tiles.keys())
                    end = 0
                    for pts in pts_sorted:
                        a = max(0, pts - core.settings.tolerance_ms)
                        b = min(duration_ms, pts + core.settings.tolerance_ms)
                        covered_ms += max(0, b - max(end, a))
                        end = max(end, b)
            tol = core.settings.tolerance_ms
            if duration_ms > 0 and pts_sorted:
                pgaps = ([pts_sorted[0]]
                         + [pts_sorted[i + 1] - pts_sorted[i] for i in range(len(pts_sorted) - 1)]
                         + [max(0, duration_ms - pts_sorted[-1])])
                max_point_gap_ms = max(pgaps)
                ugaps = ([max(0, pts_sorted[0] - tol)]
                         + [max(0, (pts_sorted[i + 1] - tol) - (pts_sorted[i] + tol))
                            for i in range(len(pts_sorted) - 1)]
                         + [max(0, duration_ms - (pts_sorted[-1] + tol))])
                max_uncovered_gap_ms = max(ugaps)
            else:
                max_point_gap_ms = duration_ms
                max_uncovered_gap_ms = duration_ms
            with src.lock:
                scheduler = src.scheduler
                n_total = scheduler.n if scheduler else 0
                g60 = {k for k in range(0, n_total, 6)} if n_total > 0 else set()
                g30 = {k for k in range(0, n_total, 3)} if n_total > 0 else set()
                cov_60s = (len(tile_keys & g60) / len(g60)) if g60 else 0.0
                cov_30s = (len(tile_keys & g30) / len(g30)) if g30 else 0.0
                upstream_error = src.proxy_src.last_error if src.proxy_src else None
                reason = upstream_error.reason if upstream_error else None
                if reason and not re.fullmatch(r"[a-z_]+|http_[0-9]{3}", reason):
                    reason = "other"
                sources.append({
                    "source": src.fp[:16], "running": src.running, "contributed": src.contributed,
                    "error": src.error,
                    "upstream_error_reason": reason,
                    "total": n_total,
                    "done": len(scheduler.done) if scheduler else 0,
                    "failed": len(scheduler.failed) if scheduler else 0,
                    "duration_ms": duration_ms, "accurate_covered_ms": covered_ms,
                    "cov_60s": round(cov_60s, 4),
                    "cov_30s": round(cov_30s, 4),
                    "max_point_gap_ms": max_point_gap_ms,
                    "max_uncovered_gap_ms": max_uncovered_gap_ms,
                    "p2_paused": scheduler.p2_paused() if scheduler else False,
                    "events": [asdict(e) for e in src.events],
                    "upstream": src.upstream.stats.snapshot() if src.upstream else None,
                    "multirange": ({"enabled": src.proxy_src.multi_enabled and src.proxy_src.planner is not None,
                                    "requests": src.proxy_src.multi_requests, "parts": src.proxy_src.multi_parts,
                                    "bytes": src.proxy_src.multi_bytes, "fallbacks": src.proxy_src.multi_fallbacks,
                                    "waits": src.proxy_src.prefetch_waits, "hits": src.proxy_src.prefetch_hits}
                                   if src.proxy_src is not None else None),
                    "guard": src.guard.stats() if src.guard is not None else None,
                    "telemetry": src.governor.stats() if src.guard is not None else None,   # what the guard hears from the player
                    "alt": core.jobs.alt_metrics(src),
                })
        return {"monotonic": core.jobs.clock(), "failed_jobs": failed_jobs, "config": {
            "bg_mbps": core.settings.bg_mbps, "pregen": core.settings.pregen,
            "thumb_quality": core.jobs.quality.name,
            "upstream_rps": core.settings.upstream_rps,
            "p2_pause_s": core.settings.p2_pause_s,
            "p1_interleave": core.settings.p1_interleave,
            "hierarchical": core.settings.hierarchical,
            "workers_per_source": core.settings.workers_per_source,
            "multirange_prefetch": core.settings.multirange_prefetch, "multirange_batch": core.settings.multirange_batch,
            "playback_guard": core.settings.playback_guard, "guard_stall_s": core.settings.guard_stall_s,
            "alt_source": core.settings.alt_source, "alt_dry_run": core.settings.alt_dry_run,
            "alt_rps": core.settings.alt_rps, "alt_mbps": core.settings.alt_mbps,
            "alt_max_conc": core.settings.alt_max_conc,
            "tolerance_ms": core.settings.tolerance_ms}, "sources": sources}

    @app.get("/admin/sharing")
    async def sharing_status():
        return {"enabled": core.sharing_enabled(), "metadata_index": core.library.enabled(),
                "public": {"listening": bool(core.settings.public_port), "port": core.settings.public_port,
                           "host": core.settings.public_host, **core.public_limiter.snapshot()},
                "invites": core.invites.listing(),
                "guests": [d for d in core.db.devices() if d["role"] == "guest"]}

    @app.delete("/admin/public/blocks", status_code=204)
    async def public_unblock():
        core.public_limiter.clear()             # lift every per-address block / strike / budget on the public listener
        return Response(status_code=204)

    @app.put("/admin/sharing")
    async def sharing_set(body: SharingIn):
        core.db.set_setting("sharing_enabled", "1" if body.enabled else "0")
        if not body.enabled:
            await asyncio.to_thread(core.jobs.stop_contributions)       # the kill switch stops guest extraction too
        return {"enabled": core.sharing_enabled()}

    @app.get("/admin/thumbnails")
    async def thumbnails_get():
        return {"quality": core.jobs.quality.name, "default": thumbq.DEFAULT.name,
                "presets": [q.public() for q in thumbq.PRESETS]}

    @app.put("/admin/thumbnails")
    async def thumbnails_set(body: ThumbQualityIn):
        """Live: no restart. Thumbnails cached at other qualities are kept (and shown until replaced)."""
        await asyncio.to_thread(core.set_thumb_quality, thumbq.BY_NAME[body.quality])
        return await thumbnails_get()

    @app.get("/admin/contributions")
    async def contributions_get():
        return {"enabled": core.db.get_setting("contrib_enabled") == "1", "effective": core.contributions_enabled(),
                "hosts": core.contrib_hosts(), "max_sources": core.settings.contrib_max_sources, "mbps": core.settings.contrib_mbps,
                "denied_hosts": sorted({h for _, h in core.contrib_denied}), "jobs": core.jobs.contributed_jobs(),
                "contributors": [d["device_id"] for d in core.db.devices() if d["contribute"]]}

    @app.put("/admin/contributions")
    async def contributions_set(body: ContribAdminIn):
        if body.enabled is not None:
            core.db.set_setting("contrib_enabled", "1" if body.enabled else "0")
            if not body.enabled:
                await asyncio.to_thread(core.jobs.stop_contributions)
        if body.hosts is not None:
            core.db.set_setting("contrib_hosts", json.dumps(clean_hosts(body.hosts)))
        return await contributions_get()

    @app.delete("/admin/contributions/{preview_id}", status_code=204)
    async def contribution_admin_stop(preview_id: str):
        if not is_preview_id(preview_id) or not await asyncio.to_thread(core.jobs.stop_contributions, None, preview_id):
            return err(404, "not_found")
        return Response(status_code=204)

    @app.put("/admin/devices/{device_id}/permissions")
    async def device_permissions(device_id: str, body: PermIn):
        if not is_device_id(device_id) or not core.db.set_contribute(device_id, body.contribute):
            return err(404, "not_found")          # unknown device, or an owner (owners never need it)
        if not body.contribute:
            await asyncio.to_thread(core.jobs.stop_contributions, device_id)
        return {"device_id": device_id, "contribute": body.contribute}

    @app.post("/admin/invites")
    async def invite_create(request: Request, body: InviteIn | None = None):
        if not core.sharing_enabled():
            return err(409, "sharing_disabled")
        b = body or InviteIn()
        if b.public and not (core.settings.public_port and core.settings.public_host):
            return err(409, "public_endpoint_not_configured")
        inv = await asyncio.to_thread(core.invites.create, b.label, b.ttl_s or 86400, "guest", b.device_days, not b.read_only)
        addrs = lan_addresses()
        host = core.settings.public_host if b.public else f"{addrs[0]}:{core.settings.port}"
        from urllib.parse import quote
        inv["address"] = host
        inv["join"] = (f"openseek://join/1?h={quote(host, safe='')}&p={quote(core.spki_pin, safe='')}"
                       f"&i={inv['invite_id']}&c={inv['secret']}")
        return inv

    @app.get("/admin/invites")
    async def invite_list():
        return {"invites": core.invites.listing()}

    @app.delete("/admin/invites/{invite_id}", status_code=204)
    async def invite_revoke(invite_id: str):
        if not is_preview_id(invite_id) or not core.invites.revoke(invite_id):
            return err(404, "not_found")
        return Response(status_code=204)

    @app.get("/admin/library")
    async def library_status():
        return {"metadata_index": core.library.enabled(), "releases": len(core.db.release_rows()),
                "stored_per_cache_entry": ["imdb_id", "tmdb_id", "season", "episode", "size_bytes (observed by the server)"],
                "never_stored": ["titles", "file names", "URLs", "headers", "client size/hash claims"]}

    @app.put("/admin/library")
    async def library_set(body: LibraryIn):
        core.library.set_enabled(body.metadata_index, purge=body.purge)
        return {"metadata_index": core.library.enabled(), "releases": len(core.db.release_rows())}

    @app.get("/admin/library/releases")
    async def library_releases():
        return {"releases": core.db.release_rows()}

    @app.put("/admin/library/entries/{fp}")
    async def library_tag(fp: str, body: EntryTagIn):
        """Identify an EXISTING cache entry (ids only; no thumbnail is touched or regenerated)."""
        from .ids import is_fingerprint
        ids = clean_ids(body.imdb_id, body.tmdb_id, body.season, body.episode)
        row = core.db.q("SELECT duration_ms FROM entries WHERE fp=?", (fp,)) if is_fingerprint(fp) else []
        if not row:
            return err(404, "not_found")
        if ids is None:
            return err(400, "bad_request")
        core.db.upsert_release_meta(fp, ids.imdb, ids.tmdb, ids.season, ids.episode, 0)
        return {"fp": fp, "imdb_id": ids.imdb, "tmdb_id": ids.tmdb, "season": ids.season, "episode": ids.episode}

    @app.delete("/admin/library/entries/{fp}", status_code=204)
    async def library_remove(fp: str):
        """Delete ONE cached title (files, rows, shares). Refused while the title is being extracted or played from."""
        from .ids import is_fingerprint
        if not is_fingerprint(fp):
            return err(404, "not_found")
        if any(getattr(s, "fp", None) == fp for s in core.jobs.running_sources()):
            return err(409, "in_use")
        if not await asyncio.to_thread(core.cache.remove_entry, fp):
            return err(404, "not_found")
        await asyncio.to_thread(core.jobs.forget_source, fp)
        return Response(status_code=204)

    @app.delete("/admin/library/releases")
    async def library_purge():
        return {"purged": core.db.purge_release_meta()}

    @app.post("/admin/alt/release")
    async def alt_release():
        return {"released": core.jobs.alt_release()}

    @app.post("/admin/pair")
    async def open_pair():
        code, ttl = await asyncio.to_thread(core.pairing.open_window)
        return {"code": code, "expires_in_s": int(ttl), "spki_pin": core.spki_pin}

    @app.delete("/admin/pair", status_code=204)
    async def close_pair():
        core.pairing.close_window()
        return Response(status_code=204)

    @app.get("/admin/devices")
    async def devices():
        return {"devices": core.db.devices()}

    @app.delete("/admin/devices/{device_id}", status_code=204)
    async def revoke(device_id: str):
        if not is_device_id(device_id):
            return err(404, "not_found")
        await asyncio.to_thread(core.jobs.revoke_device, device_id)
        if not core.auth.revoke(device_id):
            return err(404, "not_found")
        return Response(status_code=204)

    return Guard(app, core, admin=True)  # type: ignore[return-value]


# ===================================================================== serve
def uvicorn_configs(core: Core, api, admin):
    import uvicorn

    s = core.settings
    api_cfg = uvicorn.Config(api, host=s.host, port=s.port, ssl_certfile=str(core.cert_path),
                             ssl_keyfile=str(core.key_path), log_config=None, proxy_headers=False,
                             server_header=False, date_header=False, access_log=True, lifespan="off",
                             timeout_keep_alive=30, limit_concurrency=256)
    admin_cfg = uvicorn.Config(admin, host="127.0.0.1", port=s.admin_port, log_config=None, proxy_headers=False,
                               server_header=False, date_header=False, access_log=True, lifespan="off")
    # note: access_log=False on either config would silence the shared "uvicorn.access" logger
    return api_cfg, admin_cfg


def public_config(core: Core, app):
    """The internet-facing guest listener, or None when OPENSEEK_PUBLIC_PORT is not set. Same certificate and pin."""
    import uvicorn

    s = core.settings
    if not s.public_port:
        return None
    return uvicorn.Config(app, host=s.public_bind, port=s.public_port, ssl_certfile=str(core.cert_path),
                          ssl_keyfile=str(core.key_path), log_config=None, proxy_headers=False, forwarded_allow_ips="",
                          server_header=False, date_header=False, access_log=True, lifespan="off",
                          timeout_keep_alive=10, limit_concurrency=64, limit_max_requests=None)


def log_banner(core: Core) -> None:
    s = core.settings
    addrs = lan_addresses() if s.host in ("0.0.0.0", "::") else [s.host]
    lines = [f"OpenSeek {__version__} started",
             f"API (HTTPS, LAN): listening on {s.host}:{s.port}; reachable at "
             + ", ".join(f"https://{a}:{s.port}" for a in addrs),
             f"TLS certificate pin: {core.spki_pin}",
             f"Admin (loopback only): http://127.0.0.1:{s.admin_port}  -  pair a TV with `openseek pair`"]
    for line in lines:
        log.info("%s", line, extra=plain())


def serve(settings: Settings) -> None:
    import uvicorn

    setup_logging(settings.log_level)
    from .hardening import published_on_error, refuse_shared_data_dir, running_in_container
    refuse_shared_data_dir(settings.data_dir)
    if running_in_container():
        why = published_on_error(os.environ.get("OPENSEEK_PUBLISHED_ON"))
        if why:
            raise SystemExit("refusing to start in a container: " + why)
    core = Core(settings).start()
    api_cfg, admin_cfg = uvicorn_configs(core, create_api_app(core), create_admin_app(core))
    servers = [uvicorn.Server(api_cfg), uvicorn.Server(admin_cfg)]
    pub_cfg = public_config(core, create_api_app(core, public=True))
    if pub_cfg is not None:
        servers.append(uvicorn.Server(pub_cfg))
        log.info("PUBLIC guest listener: %s:%d (advertised as %s)", settings.public_bind, settings.public_port,
                 settings.public_host or "(no public host set)", extra=plain())
    from .hardening import CONTAINER_NOTE, check_data_dir
    check_data_dir(core.data_dir, log.warning)
    if running_in_container():
        log.info(CONTAINER_NOTE, os.environ.get("OPENSEEK_PUBLISHED_ON", "?"))
    log_banner(core)

    async def main():
        await asyncio.gather(*(srv.serve() for srv in servers))

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        core.stop()
