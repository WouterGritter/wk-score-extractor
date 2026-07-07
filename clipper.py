"""Cut a short goal-replay clip from the StreamGrabber ring buffer and post it
to Discord as a follow-up message.

Runs entirely off the OCR loop (a daemon thread per goal) so capture never
stalls: wait a short post-roll so the clip catches the celebration, snapshot the
last N seconds of stream-copied .ts segments, re-encode small (downscaled
H.264/AAC mp4 so Discord renders an inline player and the file fits the upload
cap), upload, then clean up the temp dir.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

from teams import name as team_name

log = logging.getLogger(__name__)


_AUDIO_KBPS = 96


def auto_bitrates(max_bytes: int, seconds: float,
                  audio_kbps: int = _AUDIO_KBPS) -> tuple[str, str, str]:
    """Pick a video bitrate so the whole clip fits under `max_bytes`.

    Splits the byte budget (minus audio, minus 15% headroom for the container +
    VBV peaks) across `seconds`, so a 90s clip fits a 10 MB cap and a boosted
    server's larger cap yields a proportionally sharper clip — no fixed guess to
    outgrow. Returns (vbitrate, maxrate, bufsize) as ffmpeg-style strings.
    """
    budget_kbit = max_bytes * 8 / 1000 * 0.85
    video_kbps = max(200, (budget_kbit / max(1.0, seconds)) - audio_kbps)
    maxrate = video_kbps * 1.1
    return (f"{int(video_kbps)}k", f"{int(maxrate)}k", f"{int(maxrate * 2)}k")


def encode_clip(concat_file: str, out_path: str, *, height: int = 720,
                vbitrate: str = "750k", maxrate: str = "900k",
                bufsize: str = "1800k", abitrate: str = "96k",
                nvenc: bool = False, loglevel: str = "error",
                timeout: float = 180.0) -> bool:
    """Re-encode the concatenated segments into a small H.264/AAC mp4.

    Downscales to `height` (width auto, kept even) and caps the bitrate so the
    output fits Discord's upload limit. `nvenc=True` uses the GPU encoder
    (h264_nvenc) — needs an nvenc-capable ffmpeg build; otherwise libx264 (CPU).
    """
    vcodec = (["-c:v", "h264_nvenc", "-preset", "p4"] if nvenc
              else ["-c:v", "libx264", "-preset", "veryfast"])
    cmd = ["ffmpeg", "-nostdin", "-loglevel", loglevel, "-y",
           "-f", "concat", "-safe", "0", "-i", concat_file,
           "-vf", f"scale=-2:{height}", *vcodec,
           "-b:v", vbitrate, "-maxrate", maxrate, "-bufsize", bufsize,
           "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", abitrate,
           "-movflags", "+faststart", out_path]
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("clip encode timed out after %.0fs", timeout)
        return False
    if proc.returncode != 0 or not os.path.exists(out_path) \
            or not os.path.getsize(out_path):
        log.warning("clip encode failed (rc=%s): %s", proc.returncode,
                    proc.stderr.decode(errors="replace").strip()[-300:])
        return False
    return True


def _caption(event) -> str:
    ht = team_name(event.home_team) or "Home"
    at = team_name(event.away_team) or "Away"
    scorer = event.scorer
    who = f" — {team_name(scorer)}" if scorer and scorer not in ("home", "away") else ""
    return f"🎥 Replay{who}: {ht} {event.home}-{event.away} {at}"


def build_and_send_clip(grabber, notifier, event, *, seconds: float = 25.0,
                        postroll: float = 5.0, height: int = 720,
                        vbitrate: str | None = None, nvenc: bool = False,
                        max_bytes: int = 10 * 1024 * 1024) -> None:
    """Thread target: post-roll, snapshot, encode, upload, clean up. Never
    raises (a clip failure must not affect the monitor).

    `vbitrate` None (the default) sizes the bitrate to the cap via
    `auto_bitrates`; pass an explicit string to override.
    """
    try:
        if postroll > 0:
            time.sleep(postroll)
        snap = grabber.snapshot(seconds)
        if not snap:
            log.info("clip: no buffered video available")
            return
        concat_file, tmpdir = snap
        out_path = os.path.join(tmpdir, "clip.mp4")
        if vbitrate:
            vb, mr, bs = vbitrate, None, None
        else:
            vb, mr, bs = auto_bitrates(max_bytes, seconds)
        enc = dict(height=height, vbitrate=vb, nvenc=nvenc)
        if mr:
            enc.update(maxrate=mr, bufsize=bs)
        try:
            if not encode_clip(concat_file, out_path, **enc):
                return
            ok = notifier.send_file(out_path, content=_caption(event),
                                    max_bytes=max_bytes)
            log.info("clip %s (%.1f MB)", "sent" if ok else "not sent",
                     os.path.getsize(out_path) / 1e6)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    except Exception as e:  # daemon thread — swallow so nothing propagates
        log.warning("clip worker error: %s", e)
