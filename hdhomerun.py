"""Minimal HDHomeRun helper: discover channels and build stream URLs."""
from __future__ import annotations

import json
import urllib.request


def _get(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def get_lineup(ip: str) -> list[dict]:
    """Full channel lineup: list of {GuideNumber, GuideName, URL, ...}."""
    return _get(f"http://{ip}/lineup.json")


def discover(ip: str) -> dict:
    return _get(f"http://{ip}/discover.json")


def channel_url(ip: str, guide_number: str | int) -> str:
    """Direct MPEG-TS stream URL for a channel number, e.g. NPO 1 -> v1."""
    return f"http://{ip}:5004/auto/v{guide_number}"


def find_channel(ip: str, name: str) -> dict | None:
    """First lineup entry whose GuideName contains `name` (case-insensitive)."""
    name = name.lower()
    for c in get_lineup(ip):
        if name in c.get("GuideName", "").lower():
            return c
    return None


def resolve_url(ip: str, channel: str) -> str:
    """Accept a full URL, a channel number, or a channel name and return a
    stream URL."""
    if channel.startswith("http"):
        return channel
    if channel.replace(".", "").isdigit():
        return channel_url(ip, channel)
    c = find_channel(ip, channel)
    if not c:
        raise ValueError(f"channel {channel!r} not found in lineup of {ip}")
    return c.get("URL") or channel_url(ip, c["GuideNumber"])


if __name__ == "__main__":
    import sys
    ip = sys.argv[1] if len(sys.argv) > 1 else "10.43.70.192"
    for c in get_lineup(ip):
        print(f"{c.get('GuideNumber','?'):>6}  {c.get('GuideName','?'):<24}  {c.get('URL','')}")
