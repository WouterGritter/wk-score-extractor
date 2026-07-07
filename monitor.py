"""Live WK score monitor: read the score from an HDHomeRun channel and log +
post a Discord message on every confirmed change (including the first, e.g. 0-0).

Polling (CPU-friendly for 24/7): IDLE polls one frame per `--interval` s; once a
scorebug is seen it goes ACTIVE (persistent stream, back-to-back OCR); after
`--timeout` min with no score it returns to IDLE. (EPG gating is planned later.)

    python3 monitor.py --ip 10.43.70.192 --channel 1
    python3 monitor.py --analyze /path/to/frame.png   # dry run, no capture
"""
from __future__ import annotations

import argparse
import logging
import os
import threading
import time

from aggregator import ScoreTracker, format_event
from capture import CaptureError, StreamGrabber, grab_frame
from clipper import build_and_send_clip
from hdhomerun import resolve_url
from notifier import DiscordNotifier
from reader_rapidocr import RapidOcrScoreReader

log = logging.getLogger("monitor")


def load_dotenv(filename: str = ".env") -> None:
    """Load KEY=VALUE lines from a .env beside this script (env vars win)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def _truthy(val: str) -> bool:
    return val.lower() in ("1", "true", "yes", "on")


def use_cuda_enabled(args) -> bool:
    """CUDA on if --cuda is passed or USE_CUDA is truthy in the env/.env."""
    return bool(args.cuda) or _truthy(os.environ.get("USE_CUDA", ""))


def hwaccel_enabled(args) -> bool:
    """GPU (NVDEC) decode on if --hwaccel is passed or FFMPEG_HWACCEL is truthy."""
    return bool(args.hwaccel) or _truthy(os.environ.get("FFMPEG_HWACCEL", ""))


def clip_enabled(args) -> bool:
    """Goal-replay clips on if --clip is passed or CLIP_ENABLE is truthy."""
    return bool(args.clip) or _truthy(os.environ.get("CLIP_ENABLE", ""))


def clip_nvenc_enabled(args) -> bool:
    """Encode clips on the GPU (h264_nvenc) if --clip-nvenc or CLIP_NVENC."""
    return bool(args.clip_nvenc) or _truthy(os.environ.get("CLIP_NVENC", ""))


def _env_or(name: str, fallback):
    """Env var wins over the argparse default (env/.env are read after parse)."""
    val = os.environ.get(name)
    return val if val not in (None, "") else fallback


def analyze(paths: list[str], use_cuda: bool = False) -> None:
    """Dry run: read the score from local image file(s) and print results."""
    reader = RapidOcrScoreReader(use_cuda=use_cuda)
    for path in paths:
        try:
            print(f"{os.path.basename(path):32s} {reader.read(path)}")
        except Exception as e:
            print(f"{os.path.basename(path):32s} error: {e}")


def run_live(args) -> None:
    url = resolve_url(args.ip, args.channel)
    reader = RapidOcrScoreReader(use_cuda=use_cuda_enabled(args))
    hwaccel = hwaccel_enabled(args)
    tracker = ScoreTracker(confirm_k=args.confirm)
    notifier = DiscordNotifier(args.webhook or os.environ.get("DISCORD_WEBHOOK_URL"))

    idle_interval = args.interval
    timeout_s = args.timeout * 60.0
    heartbeat_s = args.heartbeat * 60.0

    # --- goal-replay clip config (env overrides the argparse defaults) ---
    clips = clip_enabled(args)
    clip_seconds = float(_env_or("CLIP_SECONDS", args.clip_seconds))
    clip_postroll = float(_env_or("CLIP_POSTROLL", args.clip_postroll))
    clip_height = int(_env_or("CLIP_HEIGHT", args.clip_height))
    clip_bitrate = str(_env_or("CLIP_BITRATE", args.clip_bitrate))
    clip_max_bytes = int(float(_env_or("CLIP_MAX_MB", args.clip_max_mb)) * 1024 * 1024)
    clip_nvenc = clip_nvenc_enabled(args)
    buffer_dir = _env_or("CLIP_BUFFER_DIR", args.clip_buffer_dir)

    grabber_kwargs = {"fps": args.fps, "hwaccel": hwaccel}
    if clips:
        grabber_kwargs["buffer_dir"] = buffer_dir
        # Keep ~3x the clip length in the ring so a slow encode never races the
        # segment_wrap overwriting the segments a snapshot is copying out.
        grabber_kwargs["buffer_seconds"] = clip_seconds * 3
        log.info("goal clips ON: last %.0fs, +%.0fs post-roll, %dp @%s, "
                 "nvenc=%s, buffer=%s", clip_seconds, clip_postroll,
                 clip_height, clip_bitrate, clip_nvenc, buffer_dir)

    active = False
    last_activity = 0.0
    last_heartbeat = 0.0
    grabber: StreamGrabber | None = None
    last_seq = 0

    log.info("Monitoring %s", url)
    log.info("idle poll %.0fs | confirm x%d | match-end after %.0f min no score",
             idle_interval, args.confirm, args.timeout)

    try:
        while True:
            cycle_start = time.monotonic()

            # --- capture: persistent stream while ACTIVE, one-shot while IDLE ---
            result = None
            try:
                if active:
                    if grabber is None or not grabber.alive():
                        log.warning("stream ended — restarting")
                        if grabber:
                            grabber.stop()
                        grabber = StreamGrabber(url, **grabber_kwargs).start()
                        last_seq = 0
                    frame, last_seq = grabber.read(timeout=20.0, after_seq=last_seq)
                else:
                    frame = grab_frame(url, seek=args.seek, hwaccel=hwaccel)
                result = reader.read(frame)
            except CaptureError as e:
                log.warning("capture error: %s", e)
            except Exception as e:
                log.warning("read error: %s", e)

            if result is not None:
                log.debug("read %s", result)
                event = tracker.update(result)
                if event is not None:
                    msg = format_event(event)
                    log.info("%s", msg)
                    notifier.send(msg)
                    # A real goal (not the first-sighting "start" event) with a
                    # running ring buffer: cut + post a replay off-thread so the
                    # OCR loop keeps going while ffmpeg re-encodes and uploads.
                    if clips and grabber is not None and not event.is_first:
                        threading.Thread(
                            target=build_and_send_clip,
                            args=(grabber, notifier, event),
                            kwargs=dict(seconds=clip_seconds,
                                        postroll=clip_postroll,
                                        height=clip_height,
                                        vbitrate=(None if clip_bitrate in ("", "auto")
                                                  else clip_bitrate),
                                        nvenc=clip_nvenc, max_bytes=clip_max_bytes),
                            daemon=True).start()

                now = time.monotonic()
                if result.present and not active:
                    active = True
                    last_activity = now
                    last_heartbeat = now
                    grabber = StreamGrabber(url, **grabber_kwargs).start()
                    last_seq = 0
                    log.info("scorebug detected -> ACTIVE (persistent stream)")
                if result.score is not None:
                    last_activity = now

            now = time.monotonic()
            if active and (now - last_activity) > timeout_s:
                active = False
                if grabber:
                    grabber.stop()
                    grabber = None
                tracker.reset()
                log.info("no score for %.0f min -> match ended, back to IDLE (1/%.0fs)",
                         args.timeout, idle_interval)
            elif active and heartbeat_s and (now - last_heartbeat) > heartbeat_s:
                last_heartbeat = now
                score = tracker.confirmed
                log.info("still ACTIVE — current score %s",
                         f"{score[0]}-{score[1]}" if score else "unknown")

            if args.once:
                break
            if not active:
                time.sleep(max(0.0, idle_interval - (time.monotonic() - cycle_start)))
            # ACTIVE: no sleep — OCR + the fresh-frame wait pace the loop.
    finally:
        if grabber:
            grabber.stop()


def main() -> None:
    ap = argparse.ArgumentParser(description="Live WK score monitor")
    ap.add_argument("--ip", default="10.43.70.192", help="HDHomeRun IP")
    ap.add_argument("--channel", default="1",
                    help="channel number, name, or full stream URL")
    ap.add_argument("--interval", type=float, default=60.0,
                    help="IDLE seconds between captures (default 60)")
    ap.add_argument("--seek", type=float, default=2.0,
                    help="IDLE seconds to skip per one-shot capture (HEVC sync)")
    ap.add_argument("--fps", type=int, default=4,
                    help="ACTIVE persistent-stream frame rate (default 4)")
    ap.add_argument("--confirm", type=int, default=2,
                    help="identical reads needed to confirm a score (default 2)")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="minutes with no score before match is 'ended' (default 30)")
    ap.add_argument("--heartbeat", type=float, default=5.0,
                    help="minutes between ACTIVE heartbeat logs (0 disables)")
    ap.add_argument("--webhook", default=None,
                    help="Discord webhook URL (else env DISCORD_WEBHOOK_URL)")
    ap.add_argument("--cuda", action="store_true",
                    help="run OCR on GPU via onnxruntime-gpu (or set USE_CUDA=1)")
    ap.add_argument("--hwaccel", action="store_true",
                    help="decode on GPU via NVDEC (or set FFMPEG_HWACCEL=1); "
                         "needs cuda-enabled ffmpeg + the NVIDIA 'video' capability")
    ap.add_argument("--clip", action="store_true",
                    help="on each goal, post a short replay video (or CLIP_ENABLE=1)")
    ap.add_argument("--clip-seconds", type=float, default=25.0,
                    help="replay clip length in seconds before the goal (default 25)")
    ap.add_argument("--clip-postroll", type=float, default=5.0,
                    help="seconds to wait after a goal before cutting the clip, "
                         "so it catches the celebration (default 5)")
    ap.add_argument("--clip-height", type=int, default=720,
                    help="replay clip height in px, width auto (default 720)")
    ap.add_argument("--clip-bitrate", default="auto",
                    help="replay clip video bitrate; 'auto' sizes it to the cap "
                         "so the clip always fits (default auto)")
    ap.add_argument("--clip-max-mb", type=float, default=10.0,
                    help="skip upload if the clip exceeds this many MB "
                         "(Discord cap: 10 non-boosted, 25/100 boosted)")
    ap.add_argument("--clip-nvenc", action="store_true",
                    help="encode clips on GPU via h264_nvenc (or CLIP_NVENC=1)")
    ap.add_argument("--clip-buffer-dir", default="/dev/shm/wk-score-buffer",
                    help="ring-buffer dir for replay segments; put it on a tmpfs "
                         "(default /dev/shm/wk-score-buffer)")
    ap.add_argument("--verbose", action="store_true", help="log every frame read")
    ap.add_argument("--test", action="store_true",
                    help="send a Discord test message and exit")
    ap.add_argument("--once", action="store_true", help="one capture then exit")
    ap.add_argument("--analyze", "--dry-run", nargs="+", metavar="PNG",
                    dest="analyze",
                    help="analyse local PNG frame(s) instead of live capture")
    args = ap.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S")
    logging.getLogger("PIL").setLevel(logging.INFO)  # hide Pillow's chunk logs

    if args.test:
        webhook = args.webhook or os.environ.get("DISCORD_WEBHOOK_URL")
        ok = DiscordNotifier(webhook).send(
            "✅ wk-score-extractor test message — webhook is working.")
        log.info("test message %s", "sent" if ok else "NOT sent")
        return
    if args.analyze:
        analyze(args.analyze, use_cuda=use_cuda_enabled(args))
        return
    run_live(args)


if __name__ == "__main__":
    main()
