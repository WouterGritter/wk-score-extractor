"""Discord webhook notifier (stdlib only)."""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from uuid import uuid4

log = logging.getLogger(__name__)

# Discord's default upload cap for a non-boosted server (Tier 1 = 25 MB, Tier 3
# = 100 MB — raise --clip-max-mb / CLIP_MAX_MB if the target server is boosted).
_UA = "wk-score-extractor (https://github.com, 1.0)"


class DiscordNotifier:
    """Posts plain-text messages to a Discord webhook. A falsy URL disables
    sending (console-only mode), so the app runs fine without a webhook."""

    def __init__(self, webhook_url: str | None):
        self.url = webhook_url or None
        if not self.url:
            log.info("no Discord webhook configured — console only")

    def send(self, content: str) -> bool:
        if not self.url:
            return False
        data = json.dumps({"content": content}).encode("utf-8")
        req = urllib.request.Request(
            self.url, data=data,
            headers={"Content-Type": "application/json",
                     # Discord rejects the default urllib UA with 403.
                     "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 204):
                    log.warning("Discord webhook returned HTTP %s", resp.status)
                    return False
            return True
        except Exception as e:  # never let a webhook failure crash the monitor
            log.warning("Discord send failed: %s", e)
            return False

    def send_file(self, path: str, content: str | None = None,
                  max_bytes: int = 10 * 1024 * 1024) -> bool:
        """Upload a file (e.g. a goal-replay mp4) as its own webhook message,
        with an optional text caption. Discord renders an inline player for
        H.264/AAC mp4 under the server's size cap. Returns False (and skips the
        upload) if the file is missing or over `max_bytes`."""
        if not self.url:
            return False
        try:
            size = os.path.getsize(path)
        except OSError as e:
            log.warning("clip not found: %s", e)
            return False
        if max_bytes and size > max_bytes:
            log.warning("clip %.1f MB over Discord cap %.0f MB — not sending",
                        size / 1e6, max_bytes / 1e6)
            return False
        with open(path, "rb") as f:
            data = f.read()
        boundary = "----wk" + uuid4().hex
        body = self._multipart(boundary, content, os.path.basename(path), data)
        req = urllib.request.Request(
            self.url, data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                     "User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                if resp.status not in (200, 204):
                    log.warning("Discord file upload returned HTTP %s", resp.status)
                    return False
            return True
        except Exception as e:
            log.warning("Discord file upload failed: %s", e)
            return False

    @staticmethod
    def _multipart(boundary: str, content: str | None, filename: str,
                   data: bytes) -> bytes:
        """Build a multipart/form-data body: a `payload_json` field plus the
        file as `files[0]` (Discord's webhook attachment format)."""
        crlf = b"\r\n"
        b = boundary.encode()
        buf = bytearray()
        buf += b"--" + b + crlf
        buf += b'Content-Disposition: form-data; name="payload_json"' + crlf + crlf
        buf += json.dumps({"content": content} if content else {}).encode() + crlf
        buf += b"--" + b + crlf
        buf += (f'Content-Disposition: form-data; name="files[0]"; '
                f'filename="{filename}"').encode() + crlf
        buf += b"Content-Type: video/mp4" + crlf + crlf
        buf += data + crlf
        buf += b"--" + b + b"--" + crlf
        return bytes(buf)
