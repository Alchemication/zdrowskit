"""Loopback HTTP receiver serving the authenticated Auto Export ingest API."""

from __future__ import annotations

import json
import logging
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from http_ingest import (
    BATCH_CAPTURE_PATH,
    HEALTH_PATH,
    UPLOAD_PATH,
    HttpIngestManager,
    IngestError,
    TokenRegistry,
    validate_upload,
)

logger = logging.getLogger(__name__)


class _IngestServer(ThreadingHTTPServer):
    """Threading server carrying receiver dependencies for request handlers."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        registry: TokenRegistry,
        manager: HttpIngestManager,
        max_bytes: int,
    ) -> None:
        """Initialize the receiver server."""
        self.registry = registry
        self.manager = manager
        self.max_bytes = max_bytes
        self._request_slots = threading.BoundedSemaphore(8)
        super().__init__(address, _IngestHandler)

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        """Accept a connection with a bounded header/body read timeout."""
        request, address = super().get_request()
        request.settimeout(30)
        return request, address

    def process_request(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        """Bound concurrent request threads for this small-family service."""
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: tuple[str, int],
    ) -> None:
        """Release a request slot after the worker thread completes."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class _IngestHandler(BaseHTTPRequestHandler):
    """Minimal authenticated HTTP API for Auto Export."""

    server: _IngestServer
    protocol_version = "HTTP/1.1"

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        """Send one compact JSON response."""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _reject(self, status: int, message: str, *, profile: str = "") -> None:
        """Log why one upload was refused, then return that reason to the client."""
        logger.warning(
            "Rejected upload%s: HTTP %d %s",
            f" for {profile}" if profile else "",
            status,
            message,
        )
        self._send_json(status, {"error": message})

    def do_GET(self) -> None:  # noqa: N802
        """Serve only the receiver health endpoint."""
        if self.path != HEALTH_PATH:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        self._send_json(HTTPStatus.OK, {"status": "ok"})

    def do_POST(self) -> None:  # noqa: N802
        """Authenticate, validate, and durably stage an Auto Export payload."""
        if self.path not in (UPLOAD_PATH, BATCH_CAPTURE_PATH):
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if separator != " " or scheme.lower() != "bearer":
            self._reject(
                HTTPStatus.UNAUTHORIZED,
                "invalid token",
            )
            return
        profile_name = self.server.registry.authenticate(token.strip())
        if profile_name is None:
            self._reject(HTTPStatus.UNAUTHORIZED, "invalid token")
            return
        profile = self.server.manager.profiles.get(profile_name)
        if profile is None or not profile.enabled or profile.import_source != "http":
            logger.warning(
                "Token for profile %s is valid but that profile is not being served. "
                "Confirm it is enabled with import_source = 'http', then restart the "
                "daemon so the receiver picks up the roster change.",
                profile_name,
            )
            self._reject(HTTPStatus.UNAUTHORIZED, "invalid token")
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            self._reject(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
                profile=profile.name,
            )
            return
        raw_length = self.headers.get("Content-Length")
        try:
            content_length = int(raw_length or "")
        except ValueError:
            content_length = -1
        if content_length <= 0:
            self._reject(
                HTTPStatus.LENGTH_REQUIRED,
                "A positive Content-Length is required",
                profile=profile.name,
            )
            return
        if content_length > self.server.max_bytes:
            self._reject(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"Payload exceeds {self.server.max_bytes} bytes",
                profile=profile.name,
            )
            return
        self.connection.settimeout(30)
        body = self.rfile.read(content_length)
        if len(body) != content_length:
            self._reject(
                HTTPStatus.BAD_REQUEST,
                "Request body ended before Content-Length bytes arrived",
                profile=profile.name,
            )
            return
        headers = {key.lower(): value for key, value in self.headers.items()}
        if self.path == BATCH_CAPTURE_PATH:
            # Inspection-only dead end: no validation, no staging, no import.
            # Deliberately placed after the auth, content-type, and byte-ceiling
            # checks above so an unauthenticated caller cannot write to disk.
            try:
                captured = self.server.manager.capture_batch(
                    profile.name, headers, body
                )
            except OSError:
                logger.exception("Batch capture failed for profile %s", profile.name)
                self._send_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"error": "Receiver could not store the capture"},
                )
                return
            self._send_json(HTTPStatus.OK, captured)
            return
        try:
            upload = validate_upload(headers, body)
            accepted = self.server.manager.accept(profile.name, upload, body)
        except IngestError as exc:
            self._reject(exc.status, str(exc), profile=profile.name)
            return
        except (OSError, ValueError):
            logger.exception("HTTP ingest failed for profile %s", profile.name)
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "Receiver could not store the upload; check daemon logs"},
            )
            return
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "accepted": accepted.kind,
                "duplicate": accepted.duplicate,
                "pair_ready": accepted.pair_ready,
            },
        )

    def log_message(self, format: str, *args: object) -> None:
        """Log the raw request line at debug; outcomes are logged explicitly."""
        logger.debug("HTTP ingest: " + format, *args)

    def log_error(self, format: str, *args: object) -> None:
        """Surface protocol-level failures that never reach a handler."""
        logger.warning("HTTP ingest: " + format, *args)


class HttpIngestServer:
    """Lifecycle wrapper for the loopback HTTP receiver thread."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        registry: TokenRegistry,
        manager: HttpIngestManager,
        max_bytes: int,
    ) -> None:
        """Initialize a receiver that is started explicitly with start()."""
        if host != "127.0.0.1":
            raise ValueError(
                "HTTP ingest must bind to 127.0.0.1 behind the HTTPS proxy."
            )
        self._server = _IngestServer(
            (host, port),
            registry=registry,
            manager=manager,
            max_bytes=max_bytes,
        )
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        """Return the actual bound host and port."""
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        """Start serving requests on a background thread."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name="http-ingest",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the receiver and close its listening socket."""
        if self._thread is None:
            self._server.server_close()
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)
        self._thread = None
