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
import time

from aggregator import ScoreTracker, format_event
from capture import CaptureError, StreamGrabber, grab_frame
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
                        grabber = StreamGrabber(url, fps=args.fps,
                                                hwaccel=hwaccel).start()
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

                now = time.monotonic()
                if result.present and not active:
                    active = True
                    last_activity = now
                    last_heartbeat = now
                    grabber = StreamGrabber(url, fps=args.fps,
                                            hwaccel=hwaccel).start()
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
