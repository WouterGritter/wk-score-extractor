# wk-score-extractor

Extract the live football score from the NOS FIFA WK 2026 broadcast (captured
from an HDHomeRun) and send a Discord message when the score changes.

## Status

- [x] **Score extraction engine** — validated on ~300 frames from
  NED–SWE (final 5-1). Reads every score state correctly, rejects junk
  (close-ups, ads), zero spurious digit misreads.
- [x] **Multi-match validation** — 5 broadcasts (NED-JPN/SWE/MAR,
  CAN-MAR, PAR-FRA), 10 frames each: 100 % score accuracy across different
  teams, no per-match tuning. (Team codes are best-effort: e.g. Canada's
  possession dot makes `CAN` read as `OCA` — does not affect the score.)
- [x] **HDHomeRun live capture** — `capture.py` + `hdhomerun.py` +
  `monitor.py`. Polls a channel, reads each frame, prints to console. Also a
  `--analyze` dry-run mode for local PNGs.
- [x] **Temporal aggregation** — `aggregator.py`: confirms a score over K reads,
  stays monotonic within a match (ignores halftime-highlight replays), emits on
  first sighting (incl. 0-0) and every confirmed change; resets between matches.
- [x] **Discord webhook** — `notifier.py`: posts a message on every event.
- [x] **Goal-replay clips** — `clipper.py`: on each goal, cut ~30 s (25 s before
  + 5 s after) from an in-RAM ring buffer (same tuner, stream-copied — no extra
  decode), re-encode to 720p, and post it to Discord as a follow-up video.
  Validated live on NPO 1.
- [ ] EPG gating (skip OCR / release tuner when no football is scheduled) —
  *parked*; the IDLE 1-min baseline always runs for now.

### Running it

Put the Discord webhook in `.env` (copy `.env.example`, fill it in) — it is
loaded automatically. `--webhook URL` overrides it; with neither, it runs
console-only. `.env` is gitignored (it holds a secret).

```bash
cp .env.example .env                                # then edit in your webhook
python3 monitor.py --ip 10.43.70.192 --channel 1    # live, 24/7
python3 monitor.py --test                           # send a Discord test message
python3 monitor.py --analyze /path/to/frame.png     # dry run, no capture
```

Console logs + a Discord message fire on the first score seen (e.g. 0-0) and on
every goal. `--verbose` logs every frame read.

#### Docker

```bash
cp .env.example .env            # set DISCORD_WEBHOOK_URL (and optionally HDHR_IP / CHANNEL)
docker compose up -d --build    # runs 24/7, restarts unless stopped
docker compose logs -f          # follow events
```

`.env` supplies the webhook and (optionally) `HDHR_IP` / `CHANNEL`, which the
compose `command` passes to the monitor.

The **default** image (`Dockerfile` / `docker-compose.yml`) is the GPU build:
CUDA 12 + `onnxruntime-gpu`, `USE_CUDA=1`. It needs the NVIDIA driver +
`nvidia-container-toolkit` on the host (and that host must reach the HDHomeRun on
the LAN). Outside Docker, pass `--cuda` (or set `USE_CUDA=1`) with
`onnxruntime-gpu` installed.

**CPU-only (e.g. the laptop):** use the CPU image, which has no GPU
requirements:

```bash
docker compose -f docker-compose.cpu.yml up -d --build
```

This builds `Dockerfile.cpu` (plain `onnxruntime`, no CUDA).

### Polling (CPU-friendly for 24/7)

* **IDLE** — nothing on screen: one ffmpeg grab per `--interval` s (default 60),
  tuner released between grabs.
* **ACTIVE** — a scorebug was seen: one persistent ffmpeg stays tuned and a
  background thread keeps the latest decoded frame ready, so each cycle is just
  OCR (~2 s) instead of re-paying the ~3 s tune-in. Tune-in is paid once.
* After `--timeout` min (default 30) with no score, the match is assumed ended:
  the persistent stream is stopped (tuner freed) and it returns to IDLE.

Tuning: `--confirm K` (reads to confirm a change, default 2), `--fps` (ACTIVE
stream rate, default 4), `--seek` (IDLE HEVC sync), `--heartbeat` (ACTIVE status
log interval, min; 0 disables).

### Goal-replay clips

With `--clip` (or `CLIP_ENABLE=1`), each goal also posts a short **replay video**
to Discord as a follow-up message. The ACTIVE ffmpeg gets a second output that
stream-copies the broadcast into a rolling ring of `.ts` segments on a **tmpfs
(RAM)** — one tuner, no extra decode, no disk writes. On a goal the monitor waits
`--clip-postroll` s (default 5, to catch the celebration), cuts the last
`--clip-seconds` (default 25) from the ring, re-encodes it to 720p (H.264/AAC mp4
so Discord shows an inline player), and uploads it off the OCR thread.

`--clip-bitrate auto` (default) sizes the bitrate to `--clip-max-mb` (default 10,
Discord's non-boosted cap; raise to 25/100 on a boosted server) so the clip always
fits. `--clip-nvenc` (or `CLIP_NVENC=1`) encodes on the GPU. Both compose files
**enable clips by default** and mount the tmpfs; see `.env.example` for all knobs.

### HDHomeRun

Device: HDHomeRun FLEX QUATRO @ `10.43.70.192`, 4 tuners, DVB-T (antenna),
HEVC HD. NOS FIFA WK matches air on **NPO 1** (`.../auto/v1`). Stream URLs are
`http://<ip>:5004/auto/v<channel>`.

## How the engine works

The scorebug is a fixed top-left graphic:

```
[clock]  [flag] NED  <home>  (FIFA trophy)  <away>  SWE [flag]
```

`ScoreReader` (see `score_reader.py`) is the swappable interface:

```python
reader.read(frame) -> ScoreResult   # frame = path | PIL.Image | numpy RGB
```

`ScoreResult` carries `present`, `home`, `away`, `home_team`, `away_team`,
`clock`, `confidence`, and a `.score` -> `(home, away) | None` helper.

The default implementation `RapidOcrScoreReader` (`reader_rapidocr.py`):

1. Crops a generous **top-left ROI** (fractional, not pixel-hardcoded) and
   upscales it.
2. Runs **RapidOCR** (offline, CPU, ~2 s/frame) to get text tokens with
   bounding boxes and confidences.
3. Parses the scorebug row:
   - the two **score digits** are pure-numeric tokens (the FIFA "26" trophy
     logo is dropped);
   - **team codes** are the outermost alphabetic tokens *outside* the digit
     span (so a trophy misread as letters, being central, is ignored);
   - home = leftmost digit, away = rightmost digit.

This is broadcast-font-agnostic and adapts to any pair of teams. The engine is
swappable — e.g. an Anthropic-API-based reader could implement the same
`ScoreReader` interface.

### Important real-world behaviour

During **halftime the broadcast replays highlights** using the same scorebug
with the *old* score and *old* clock. The reader correctly reads what is on
screen (e.g. 1-0 @05:08 at real-time 66 min), and `aggregator.py` filters it out:

1. **Monotonic non-decreasing** — ignores any read below the confirmed score.
2. **Confirm over K reads** — a single OCR blip never fires an event.
3. **Both team codes required** — while the scorebug animates in/out it renders
   partially (one team code missing) and a digit can be misread with *high*
   confidence (e.g. `9-0 ?-ESP`). A stable scorebug always shows both codes, so
   a read with a missing team is ignored. This catches high-confidence blips
   that a confidence threshold alone cannot. (`min_conf` 0.8 handles the rest.)

## Layout

| File | Purpose |
|------|---------|
| `score_reader.py` | `ScoreReader` interface + `ScoreResult` |
| `reader_rapidocr.py` | RapidOCR implementation |
| `capture.py` | Grab one frame from a stream via ffmpeg |
| `hdhomerun.py` | Discover channels / resolve stream URLs |
| `aggregator.py` | Temporal state machine: confirmed score changes |
| `notifier.py` | Discord webhook sender (messages + file uploads) |
| `clipper.py` | Goal-replay clip: snapshot ring buffer → re-encode → upload |
| `monitor.py` | Live poll loop + `--analyze` dry run; logs + notifies |

### Extracting test frames from a recording (fast, no full read)

`ffmpeg` input seeking (`-ss` before `-i`) jumps directly to a timestamp,
reading only a small chunk near it — so pulling a frame from a 22 GB `.ts`
over WiFi takes ~2 s, not a full-file read:

```bash
ffmpeg -nostdin -ss 3000 -i "/path/to/match.ts" -frames:v 1 -q:v 2 frame.png
```

## Requirements

`ffmpeg` on PATH, plus:

```
pip install -r requirements.txt
```
