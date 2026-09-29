"""Local web UI.

Stdlib only, same as the rest of the package. It serves a single page and a
small JSON API in front of the existing engine, so the browser gets exactly the
behaviour the CLI has: one lexicon, one set of rules, nothing ported twice.

    py -m humanizer.web

Binds to 127.0.0.1 by default. This is a personal tool on a threaded
``http.server``, not a hardened public service: if you put it on a network,
put a real reverse proxy in front of it and read ``_settings_from`` first, which
is the only place client input turns into engine configuration.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from . import __version__
from .core import Humanizer, Settings, apply_edits_with_spans

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "webui")

MAX_TEXT_BYTES = 2 * 1024 * 1024        # 2 MB of prose is ~350k words
MAX_DOCX_BYTES = 20 * 1024 * 1024
MAX_PRESERVE_PATTERNS = 10
MAX_PRESERVE_LENGTH = 200

BOOLEAN_FIELDS = (
    "em_dashes", "hyphens", "contractions", "lexical",
    "seasoning", "typos", "quotes", "semicolons", "protect_first_sentence",
)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class BadRequest(Exception):
    """Client sent something unusable; the message goes back as-is."""


# --------------------------------------------------------------------------
# Request payload -> engine settings
# --------------------------------------------------------------------------


def _settings_from(payload: Dict[str, Any]) -> Settings:
    """Build Settings from untrusted JSON.

    Whitelisted keys only, every value coerced and clamped. Nothing here should
    ever be able to hand the engine a value the CLI could not produce.
    """
    if not isinstance(payload, dict):
        raise BadRequest("settings must be an object")

    settings = Settings()

    if "level" in payload:
        try:
            settings.level = max(0.0, min(10.0, float(payload["level"])))
        except (TypeError, ValueError):
            raise BadRequest("level must be a number between 0 and 10")

    rate = payload.get("typo_rate")
    if rate is not None and rate != "":
        try:
            settings.typo_rate = max(0.0, min(200.0, float(rate)))
        except (TypeError, ValueError):
            raise BadRequest("typo_rate must be a number")

    seed = payload.get("seed")
    if seed is not None and seed != "":
        try:
            settings.seed = int(seed) & 0xFFFFFFFF
        except (TypeError, ValueError):
            raise BadRequest("seed must be a whole number")

    for field in BOOLEAN_FIELDS:
        if field in payload:
            setattr(settings, field, bool(payload[field]))

    preserve = payload.get("preserve") or []
    if not isinstance(preserve, list):
        raise BadRequest("preserve must be a list of patterns")
    if len(preserve) > MAX_PRESERVE_PATTERNS:
        raise BadRequest("at most %d preserve patterns" % MAX_PRESERVE_PATTERNS)
    cleaned = []
    for pattern in preserve:
        if not isinstance(pattern, str) or not pattern.strip():
            continue
        if len(pattern) > MAX_PRESERVE_LENGTH:
            raise BadRequest("preserve patterns are limited to %d characters"
                             % MAX_PRESERVE_LENGTH)
        try:
            re.compile(pattern)
        except re.error as error:
            raise BadRequest("bad preserve pattern %r: %s" % (pattern, error))
        cleaned.append(pattern)
    settings.preserve = cleaned

    return settings


def _describe(settings: Settings) -> Dict[str, Any]:
    payload = {field: getattr(settings, field) for field in BOOLEAN_FIELDS}
    payload.update({
        "level": settings.level,
        "typo_rate": settings.typo_rate,
        "effective_typo_rate": round(settings.effective_typo_rate(), 2),
        "seed": settings.seed,
        "preserve": settings.preserve,
    })
    return payload


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------


def humanize_text(payload: Dict[str, Any]) -> Dict[str, Any]:
    text = payload.get("text", "")
    if not isinstance(text, str):
        raise BadRequest("text must be a string")
    if len(text.encode("utf-8")) > MAX_TEXT_BYTES:
        raise BadRequest("text is larger than %d MB" % (MAX_TEXT_BYTES // 1048576))

    settings = _settings_from(payload.get("settings") or {})
    humanizer = Humanizer(settings)
    edits = humanizer.plan(text)
    result, spans = apply_edits_with_spans(text, edits)

    return {
        "text": result,
        "spans": [{"start": s, "end": e, "kind": k, "old": o}
                  for s, e, k, o in spans],
        "stats": humanizer.stats,
        "words": len(re.findall(r"[A-Za-z']+", text)),
        "settings": _describe(settings),
    }


def humanize_document(data: bytes, payload: Dict[str, Any]) -> Dict[str, Any]:
    from .docxio import humanize_docx

    if len(data) > MAX_DOCX_BYTES:
        raise BadRequest("document is larger than %d MB"
                         % (MAX_DOCX_BYTES // 1048576))
    if not data.startswith(b"PK"):
        raise BadRequest("that is not a .docx file")

    settings = _settings_from(payload.get("settings") or {})
    humanizer = Humanizer(settings)

    workspace = tempfile.mkdtemp(prefix="humanizer-")
    try:
        source = os.path.join(workspace, "in.docx")
        destination = os.path.join(workspace, "out.docx")
        with open(source, "wb") as handle:
            handle.write(data)
        try:
            report = humanize_docx(source, destination, humanizer,
                                   include_comments=bool(payload.get("comments")))
        except ValueError as error:
            raise BadRequest(str(error))
        with open(destination, "rb") as handle:
            rewritten = handle.read()
    finally:
        shutil.rmtree(workspace, ignore_errors=True)

    return {
        "file": base64.b64encode(rewritten).decode("ascii"),
        "name": _output_name(payload.get("name")),
        "stats": report.stats,
        "words": report.words,
        "paragraphs": report.paragraphs,
        "rewritten": report.rewritten,
        "changes": [{"before": before, "after": after}
                    for before, after in report.changes[:60]],
        "truncated": len(report.changes) > 60,
        "settings": _describe(settings),
    }


def _output_name(name: Optional[str]) -> str:
    if not isinstance(name, str) or not name.strip():
        return "humanized.docx"
    # the client picked this, so strip anything path-shaped out of it
    base = os.path.basename(name.replace("\\", "/")).strip() or "document.docx"
    stem, extension = os.path.splitext(base)
    if extension.lower() not in (".docx", ".docm"):
        extension = ".docx"
    return (stem or "document")[:80] + ".humanized" + extension


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "humanizer/" + __version__
    protocol_version = "HTTP/1.1"
    quiet = False

    # -- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args):
        if not self.quiet:
            sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str,
              extra: Optional[Dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # everything is served from this origin; no external fetches at all
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self'; script-src 'self'; connect-src 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def _read_body(self, limit: int) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise BadRequest("bad Content-Length")
        if length > limit:
            raise BadRequest("request body is too large")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    # -- routes ---------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/defaults":
            self._send_json(200, {
                "version": __version__,
                "settings": _describe(Settings()),
                "max_text_bytes": MAX_TEXT_BYTES,
                "max_docx_bytes": MAX_DOCX_BYTES,
            })
            return
        self._serve_static(path)

    do_HEAD = do_GET

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/humanize":
                payload = self._json_body(MAX_TEXT_BYTES + 8192)
                self._send_json(200, humanize_text(payload))
            elif path == "/api/docx":
                payload = self._json_body(int(MAX_DOCX_BYTES * 1.4) + 8192)
                blob = payload.get("file")
                if not isinstance(blob, str):
                    raise BadRequest("no file was sent")
                try:
                    data = base64.b64decode(blob, validate=True)
                except Exception:
                    raise BadRequest("the file could not be decoded")
                self._send_json(200, humanize_document(data, payload))
            else:
                self._error(404, "no such endpoint")
        except BadRequest as error:
            self._error(400, str(error))
        except Exception as error:                      # noqa: BLE001
            self.log_message("unhandled: %s: %s", type(error).__name__, error)
            self._error(500, "%s: %s" % (type(error).__name__, error))

    def _json_body(self, limit: int) -> Dict[str, Any]:
        raw = self._read_body(limit)
        if not raw:
            raise BadRequest("empty request")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise BadRequest("body was not valid JSON")
        if not isinstance(payload, dict):
            raise BadRequest("body must be a JSON object")
        return payload

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, relative))
        # normpath first, then confirm we are still inside the static root
        if os.path.commonpath([os.path.abspath(target), STATIC_DIR]) != STATIC_DIR:
            self._error(403, "forbidden")
            return
        if not os.path.isfile(target):
            self._error(404, "not found")
            return
        with open(target, "rb") as handle:
            body = handle.read()
        extension = os.path.splitext(target)[1].lower()
        self._send(200, body, CONTENT_TYPES.get(extension, "application/octet-stream"),
                   {"Cache-Control": "no-cache"})


def serve(host: str = "127.0.0.1", port: int = 8000, open_browser: bool = True,
          quiet: bool = False) -> None:
    Handler.quiet = quiet
    server = ThreadingHTTPServer((host, port), Handler)
    shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
    url = "http://%s:%d/" % (shown, server.server_address[1])

    print("humanizer %s" % __version__)
    print("  %s" % url)
    if host not in ("127.0.0.1", "localhost"):
        print("  reachable from the network: anyone who can see this port can use it")
    print("  ctrl-c to stop")

    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="humanize-web", description="Serve the humanizer web UI locally.")
    parser.add_argument("-p", "--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="default 127.0.0.1; use 0.0.0.0 to expose it")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open a browser window")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="do not log requests")
    args = parser.parse_args(argv)
    try:
        serve(args.host, args.port, not args.no_browser, args.quiet)
    except OSError as error:
        sys.stderr.write("cannot serve on %s:%d: %s\n"
                         % (args.host, args.port, error))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
