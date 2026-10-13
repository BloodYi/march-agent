"""Desktop-compatible Cloud Code client profile for the native agy runtime.

Bodies and returned model names pass through unchanged. The private loopback
bridge only selects the upstream client profile through its User-Agent header.
"""
from __future__ import annotations

import http.client
import http.server
import re
import secrets
import socket
import threading
import urllib.parse


class DesktopProfileProxy:
    def __init__(self, upstream: str = "https://daily-cloudcode-pa.googleapis.com",
                 version: str = "2.22.0", timeout: int = 1800):
        target = urllib.parse.urlsplit(upstream)
        if (target.scheme != "https" or target.hostname not in
                {"daily-cloudcode-pa.googleapis.com", "cloudcode-pa.googleapis.com"}
                or target.port not in (None, 443) or target.username or target.password
                or target.query or target.fragment or target.path.strip("/")):
            raise ValueError("Desktop profile requires an official Cloud Code HTTPS endpoint")
        if not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("Invalid AGY_DESKTOP_VERSION")
        self.host, self.version = target.hostname, version
        self.timeout = None if timeout == 0 else (timeout if timeout > 0 else 1800)
        self.prefix = "/" + secrets.token_urlsafe(32)
        self.connections: set[http.client.HTTPSConnection] = set()
        self.lock = threading.Lock()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass  # Never log authorization, bodies, or private loopback URLs.

            def do_POST(self):
                if not self.path.startswith(owner.prefix + "/v1internal:"):
                    self.send_error(404)
                    return
                connection = None
                headers_started = False
                try:
                    body = self.read_body()
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() not in {"host", "connection", "content-length",
                                                      "transfer-encoding", "accept-encoding"}}
                    native_agent = self.headers.get("User-Agent", "")
                    attributes = []
                    for name in ("os_type", "arch", "auth_method"):
                        match = re.search(r"\b" + name + r"=([A-Za-z0-9_-]+)", native_agent)
                        if match:
                            attributes.append(name + "=" + match.group(1))
                    headers = {key: value for key, value in headers.items()
                               if key.lower() != "user-agent"}
                    suffix = "; " + "; ".join(attributes) if attributes else ""
                    headers["User-Agent"] = f"antigravity/{owner.version} (aidev_client{suffix})"
                    headers["Accept-Encoding"] = "identity"
                    connection = http.client.HTTPSConnection(owner.host, timeout=owner.timeout)
                    with owner.lock:
                        owner.connections.add(connection)
                    connection.request("POST", self.path[len(owner.prefix):], body=body, headers=headers)
                    response = connection.getresponse()
                    headers_started = True
                    self.send_response(response.status)
                    for name in ("Content-Type", "Content-Encoding"):
                        if response.getheader(name):
                            self.send_header(name, response.getheader(name))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    while chunk := response.read1(65536):
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except (OSError, ValueError, http.client.HTTPException):
                    # A failed upstream must never become a fabricated successful reply.
                    if not headers_started:
                        try:
                            self.send_error(502, "Cloud Code forwarding failed")
                        except OSError:
                            pass
                finally:
                    self.close_connection = True
                    if connection is not None:
                        with owner.lock:
                            owner.connections.discard(connection)
                        connection.close()

            def read_body(self):
                limit = 64 * 1024 * 1024
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    chunks, total = [], 0
                    while True:
                        line = self.rfile.readline(128)
                        length = int(line.split(b";", 1)[0].strip(), 16)
                        if not length:
                            while self.rfile.readline(8192).strip():
                                pass
                            return b"".join(chunks)
                        total += length
                        if total > limit:
                            raise ValueError("Request too large")
                        chunk = self.rfile.read(length)
                        if len(chunk) != length or self.rfile.read(2) != b"\r\n":
                            raise ValueError("Incomplete chunked body")
                        chunks.append(chunk)
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > limit:
                    raise ValueError("Request too large")
                body = self.rfile.read(length)
                if len(body) != length:
                    raise ValueError("Incomplete request body")
                return body

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}{self.prefix}"

    def close(self):
        self.server.shutdown()
        with self.lock:
            connections = list(self.connections)
        for connection in connections:
            if connection.sock:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        self.server.server_close()
        self.thread.join(timeout=1)
