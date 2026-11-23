"""Ultra-lightweight static welcome + deep-link redirect server for Render.

Uses only Python's standard library (~12 MB RAM, zero external packages, zero
database or Discord connections) so Render free-tier resources are never
exhausted while gys.fl350r.com stays online.
"""
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

PORT = int(os.getenv("PORT", "10000"))
DATA_SITE_URL = os.getenv("DATA_SITE_URL", "https://graveyard.pimpnastie.com").rstrip("/")
DISCORD_INVITE_URL = os.getenv("DISCORD_INVITE_URL", "https://discord.gg/yS9U9mrABJ").strip()
DATA_SITE_HOST = urlparse(DATA_SITE_URL).netloc or "graveyard.pimpnastie.com"

TEMPLATE_PATH = Path(__file__).resolve().parent / "render_welcome" / "index.html"
HTML_BYTES = (
    TEMPLATE_PATH.read_text(encoding="utf-8")
    .replace("{{DATA_SITE_URL}}", DATA_SITE_URL)
    .replace("{{DATA_SITE_HOST}}", DATA_SITE_HOST)
    .replace("{{DISCORD_INVITE_URL}}", DISCORD_INVITE_URL)
    .encode("utf-8")
)


class WelcomeHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self._route(send_body=False)

    def do_GET(self):
        self._route(send_body=True)

    def _route(self, send_body: bool):
        parsed = urlparse(self.path)
        if parsed.path in ("/health", "/healthz"):
            body = b"ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)
            return

        if parsed.path in ("/", "/index.html"):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "public, max-age=300")
            self.send_header("Content-Length", str(len(HTML_BYTES)))
            self.end_headers()
            if send_body:
                self.wfile.write(HTML_BYTES)
            return

        # Preserve deep links (e.g. /player/..., /clan-history, /forum/...)
        target = f"{DATA_SITE_URL}{parsed.path}"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        self.send_response(301)
        self.send_header("Location", target)
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", PORT), WelcomeHandler)
    print(f"Graveyard welcome gateway listening on 0.0.0.0:{PORT} -> {DATA_SITE_URL}", flush=True)
    server.serve_forever()
