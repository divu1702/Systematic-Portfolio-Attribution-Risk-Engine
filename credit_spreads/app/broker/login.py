"""Morning Kite login helper.

The owner opens the login URL, completes 2FA, and Kite redirects the browser to
`broker.redirect_url` with `?request_token=...`. On the machine running the system, a tiny
local web server catches that redirect. From a phone the page will not load (it points at
127.0.0.1); copy the request_token from the address bar and pass it with
`python -m app login --token ...` or the Telegram /token command.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse


def wait_for_redirect(redirect_url: str, timeout: float = 300.0) -> Optional[str]:
    """Serve redirect_url locally until Kite redirects to it. Returns the full callback URL
    (with query string), or None on timeout."""
    u = urlparse(redirect_url)
    host, port, path = u.hostname or "127.0.0.1", u.port or 80, u.path or "/"
    got: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if urlparse(self.path).path != path:
                self.send_response(404)
                self.end_headers()
                return
            got["url"] = f"{redirect_url}?{urlparse(self.path).query}"
            ok = "request_token" in parse_qs(urlparse(self.path).query)
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "Login received. You can close this tab." if ok else "No request_token in this URL."
            self.wfile.write(f"<html><body><h3>{msg}</h3></body></html>".encode())
            done.set()

        def log_message(self, *args):  # keep the console quiet
            pass

    server = HTTPServer((host, port), Handler)
    server.timeout = 0.5
    try:
        t_end = timeout
        waited = 0.0
        while not done.is_set() and waited < t_end:
            server.handle_request()
            waited += server.timeout
    finally:
        server.server_close()
    return got.get("url")
