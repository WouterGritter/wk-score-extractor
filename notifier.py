"""Discord webhook notifier (stdlib only)."""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)


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
                     "User-Agent": "wk-score-extractor (https://github.com, 1.0)"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status not in (200, 204):
                    log.warning("Discord webhook returned HTTP %s", resp.status)
                    return False
            return True
        except Exception as e:  # never let a webhook failure crash the monitor
            log.warning("Discord send failed: %s", e)
            return False
