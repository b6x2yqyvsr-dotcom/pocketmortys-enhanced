"""HTTP plumbing: request object, router, responses, request recording.

Design notes
------------
* Routing is by exact method+path.  The client uses fixed paths with no
  parameters, and the PHP reference proved a literal lookup table is enough.
* Every request/response pair is appended to ``recordings/<date>.jsonl``.
  When you are reconstructing a protocol from a dead server this log *is* the
  specification -- it is how you discover what the client actually asked for
  and why it rejected what you sent.
* Handlers may return a dict (JSON), a :class:`Response`, or an
  :class:`SSEStream`.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
import time
import traceback
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, urlparse

from . import config, db

# --------------------------------------------------------------------------
# router
# --------------------------------------------------------------------------

Handler = Callable[..., Any]

_ROUTES: dict[tuple[str, str], Handler] = {}
_PATTERNS: list[tuple[str, re.Pattern, tuple[str, ...], Handler]] = []
_LOCK = threading.Lock()

# ``<name>`` matches exactly one path segment; ``<path:name>`` matches the
# remainder (at least one segment).  The client's static endpoints are all
# exact, so patterns exist only for the asset CDN.
_PARAM_RE = re.compile(r"<(?:path:)?([A-Za-z_][A-Za-z0-9_]*)>")


def _compile(pattern: str):
    """Turn ``/Aliases/<group>/<platform>`` into a regex + parameter names."""
    names: list[str] = []
    out = ["^"]
    pos = 0
    for m in _PARAM_RE.finditer(pattern):
        out.append(re.escape(pattern[pos:m.start()]))
        raw = m.group(0)
        names.append(m.group(1))
        if raw.startswith("<path:"):
            out.append(r"(.+)")
        else:
            out.append(r"([^/]+)")
        pos = m.end()
    out.append(re.escape(pattern[pos:]))
    out.append("/?$")
    return re.compile("".join(out)), tuple(names)


def _normalize_path(path: str) -> str:
    """Collapse ``/./`` and ``//`` runs before route matching.

    The metadata patcher rewrites leftover host strings in place, so it has to
    keep each URL exactly as long as the original.  It pads the difference with
    ``/.`` segments (``http://host:8080/././././Aliases/rat/Android``), which are
    a no-op under RFC 3986 normalisation but are NOT stripped by every client
    stack.  Accepting them here means the padded form works either way.
    """
    if "/./" not in path and "//" not in path:
        return path
    out: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if out:
                out.pop()
            continue
        out.append(segment)
    return "/" + "/".join(out)


def route(method: str, path: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        with _LOCK:
            key = (method.upper(), path.rstrip("/") or "/")
            if key in _ROUTES or any(
                k == key and p.pattern for k, p, _, _ in _PATTERNS
            ):
                raise RuntimeError(f"duplicate route {key}")
            if "<" in path:
                regex, names = _compile(path.rstrip("/") or "/")
                _PATTERNS.append((key[0], regex, names, fn))
            else:
                _ROUTES[key] = fn
        return fn
    return deco


def get(path: str):
    return route("GET", path)


def post(path: str):
    return route("POST", path)


def resolve(method: str, path: str) -> tuple[Handler, dict] | None:
    """Return (handler, path parameters) for a request, or None."""
    path = path.rstrip("/") or "/"
    handler = _ROUTES.get((method.upper(), path))
    if handler is not None:
        return handler, {}
    for verb, regex, names, fn in _PATTERNS:
        if verb != method.upper():
            continue
        match = regex.match(path)
        if match:
            return fn, dict(zip(names, match.groups()))
    return None


def lookup(method: str, path: str) -> Handler | None:
    found = resolve(method, path)
    return found[0] if found else None


def all_routes() -> list[tuple[str, str]]:
    extra = [(verb, regex.pattern) for verb, regex, _, _ in _PATTERNS]
    return sorted(list(_ROUTES) + extra)


# --------------------------------------------------------------------------
# request / response
# --------------------------------------------------------------------------

@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    body: bytes
    client: str = ""

    _json: Any = field(default=None, repr=False)
    _json_done: bool = field(default=False, repr=False)
    player: dict | None = None
    session: dict | None = None

    @property
    def json(self) -> Any:
        if not self._json_done:
            self._json_done = True
            if self.body:
                try:
                    self._json = json.loads(self.body.decode("utf-8", "replace"))
                except Exception:  # noqa: BLE001
                    self._json = None
        return self._json

    def arg(self, name: str, default: Any = None) -> Any:
        vals = self.query.get(name)
        return vals[0] if vals else default

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)


def json_response(payload: Any, status: int = 200, **headers: str) -> Response:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode()
    return Response(status=status, body=body, headers=dict(headers))


def error(code: str, status: int = 400, **extra: Any) -> Response:
    return json_response({"error": {"code": code, **extra}}, status=status)


@dataclass
class SSEStream:
    """A long-lived ``text/event-stream`` body."""
    generator: Iterator[str]
    headers: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# request recording
# --------------------------------------------------------------------------

_record_lock = threading.Lock()


def record(entry: dict) -> None:
    config.RECORD_DIR.mkdir(parents=True, exist_ok=True)
    day = time.strftime("%Y-%m-%d")
    path = config.RECORD_DIR / f"{day}.jsonl"
    try:
        with _record_lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# auth
# --------------------------------------------------------------------------

def authenticate(req: Request) -> None:
    """Populate ``req.player`` from whatever credential the client supplied.

    The client sends the session token in several different places depending
    on the call, so we accept all of them:
      * ``Authorization: Bearer <jwt>``
      * ``?token=<jwt>`` / ``?session_id=<id>``
      * ``X-Session-Id``
    """
    token = ""

    auth = req.header("authorization")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        token = req.arg("token") or ""
    if not token:
        token = req.header("x-session-id") or ""
    if not token and isinstance(req.json, dict):
        token = req.json.get("token") or req.json.get("session_id") or ""

    if token:
        payload = None
        if token.count(".") == 2:
            from .jwtutil import signer
            payload = signer.decode(token, verify=False)
            if payload is None:
                payload = None
        if payload:
            req.session = payload
            sid = payload.get("session_id")
            if sid:
                req.player = db.rowdict(
                    db.one("SELECT * FROM users WHERE session_id = ? LIMIT 1", (sid,))
                )

    if req.player is None:
        # fall back to a raw session id
        raw = req.arg("session_id") or req.header("x-session-id")
        if raw:
            req.player = db.rowdict(
                db.one("SELECT * FROM users WHERE session_id = ? LIMIT 1", (raw,))
            )


def require_player(req: Request) -> dict:
    if not req.player:
        raise HttpError(error("Not authenticated", status=401))
    return req.player


# --------------------------------------------------------------------------
# handler
# --------------------------------------------------------------------------

class HttpError(Exception):
    def __init__(self, response: Response):
        super().__init__(f"HTTP {response.status}")
        self.response = response


class Handler_(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PMNet/1.0"

    # ---- logging -----------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        if not config.VERBOSE:
            return
        # The User-Agent separates the game's own calls from the OneTrust
        # WebView's banner polling and from browser tabs on /admin -- all three
        # arrive from 127.0.0.1 and are otherwise indistinguishable.
        line = f"[http] {self.address_string()} {fmt % args}"
        ua = (self.headers.get("User-Agent") or "").strip()
        if ua:
            line += f"  ua={ua[:60]}"
        print(line)
        # Bare "GET /" with no User-Agent is the anonymous prober (the game's
        # mode switch, or the OneTrust WebView).  Dump every header so the two
        # can be told apart instead of guessed at.
        if not ua and "GET / " in f"{fmt % args}":
            for k, v in self.headers.items():
                print(f"       | {k}: {v[:110]}")

    # ---- verbs -------------------------------------------------------
    def do_GET(self) -> None:      # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:     # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:      # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:   # noqa: N802
        self._dispatch("DELETE")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._dispatch("OPTIONS")

    def do_HEAD(self) -> None:     # noqa: N802
        self._dispatch("HEAD")

    # ---- core --------------------------------------------------------
    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length:
            try:
                return self.rfile.read(int(length))
            except (ValueError, OSError):
                return b""
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                if not line:
                    break
                try:
                    size = int(line.split(b";")[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()
            return b"".join(chunks)
        return b""

    def _dispatch(self, method: str) -> None:
        started = time.time()
        parsed = urlparse(self.path)
        path = _normalize_path(parsed.path)
        body = self._read_body() if method in ("POST", "PUT", "DELETE") else b""

        headers = {k.lower(): v for k, v in self.headers.items()}
        raw = body
        if headers.get("content-encoding", "").lower() == "gzip" and body:
            try:
                raw = gzip.decompress(body)
            except OSError:
                raw = body

        req = Request(
            method=method,
            path=path,
            query=parse_qs(parsed.query),
            headers=headers,
            body=raw,
            client=self.client_address[0] if self.client_address else "",
        )
        try:
            authenticate(req)
        except Exception:  # noqa: BLE001
            pass

        found = resolve(method, path)
        if found is None:
            if method == "OPTIONS":
                self._send(Response(status=204))
                return
            resp = error("NOT_FOUND", status=404, path=path)
            self._finish(req, resp, started)
            return
        handler, path_params = found

        try:
            result = handler(req, **path_params)
        except HttpError as exc:
            self._finish(req, exc.response, started)
            return
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            print(f"[error] {method} {path}\n{tb}")
            self._finish(req, error("SERVER_ERROR", status=500, detail=tb[-800:]),
                         started)
            return

        if isinstance(result, SSEStream):
            self._serve_sse(req, result, started)
            return

        if result is None:
            result = Response(status=204)
        elif isinstance(result, (dict, list)):
            # Tuple form, not ``dict | list``: the union operator is only
            # valid at runtime from Python 3.10, and Debian 11 ships 3.9.
            result = json_response(result)

        self._finish(req, result, started)

    def _finish(self, req: Request, resp: Response, started: float) -> None:
        self._send(resp)
        record({
            "t": round(time.time(), 3),
            "dt": round((time.time() - started) * 1000, 1),
            "method": req.method,
            "path": req.path,
            "query": req.query,
            "client": req.client,
            "headers": {k: v for k, v in req.headers.items()
                        if k in ("user-agent", "content-type", "accept",
                                 "authorization", "x-session-id")},
            "req": _safe(req.json),
            "status": resp.status,
            "resp": _safe_json(resp.body),
        })

    def _send(self, resp: Response) -> None:
        body = resp.body
        headers = dict(resp.headers)

        # Who asked, and for what -- this feeds the panel's 设备 page.  Clients
        # are grouped by (address, User-Agent) because the game, the OneTrust
        # WebView and the panel itself all arrive from 127.0.0.1 under adb
        # reverse, and would otherwise collapse into one row.
        try:
            from . import telemetry
            if telemetry.get_bool("log_requests", True):
                telemetry.record(
                    address=self.address_string(),
                    method=self.command or "GET",
                    path=self.path or "/",
                    status=int(resp.status),
                    user_agent=self.headers.get("User-Agent", ""),
                    player_id=getattr(self, "_player_id", None),
                )
        except Exception:  # noqa: BLE001
            pass

        accepts_gzip = "gzip" in self.headers.get("Accept-Encoding", "")
        use_gzip = False
        if accepts_gzip and len(body) > 512 and "content-encoding" not in {
            k.lower() for k in headers
        }:
            try:
                body = gzip.compress(body, 6)
                use_gzip = True
            except Exception:  # noqa: BLE001
                body = resp.body

        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", resp.content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k, v)
            # The client is a plain HTTP stack; permissive CORS costs nothing
            # and makes browser-based debugging trivial.
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("X-Powered-By", "Express")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_sse(self, req: Request, stream: SSEStream, started: float) -> None:
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            for k, v in stream.headers.items():
                self.send_header(k, v)
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return

        events = 0
        try:
            for chunk in stream.generator:
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
                events += 1
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            record({
                "t": round(time.time(), 3),
                "dt": round((time.time() - started) * 1000, 1),
                "method": req.method,
                "path": req.path,
                "query": req.query,
                "client": req.client,
                "sse": True,
                "chunks": events,
                "req": _safe(req.json),
            })


def _safe(obj: Any, limit: int = 20000) -> Any:
    if obj is None:
        return None
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)[:limit]
    if len(text) > limit:
        return text[:limit] + "...(truncated)"
    return obj


def _safe_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return _safe(json.loads(raw.decode("utf-8", "replace")))
    except Exception:  # noqa: BLE001
        return raw[:2000].decode("utf-8", "replace")
