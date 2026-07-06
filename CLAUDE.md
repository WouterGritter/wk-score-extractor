# CLAUDE.md — technical notes for future sessions

Context and non-obvious details for working on this repo. User-facing docs are in
`README.md`; this file is the "why" and the gotchas.

## What it is

Reads the live football score from the **NOS FIFA WK 2026** broadcast (captured
from an HDHomeRun tuner), and posts a Discord message on every confirmed score
change. Runs 24/7 on the user's Linux laptop (`wouter-asus`).

Status: engine + live capture + aggregation + Discord all **done and validated
on real matches**. Parked: **EPG gating** (see bottom).

## Repo location

Primary path `/mnt/c/dev/wk-score-extractor`; `~/dev/wk-score-extractor` is a
**symlink** to it. Same files — edit either. User runs the app from `~/dev/...`.

## Architecture (data flow)

```
HDHomeRun ──ffmpeg──> frame ──ScoreReader──> ScoreResult ──ScoreTracker──> ScoreEvent ──> Discord + log
          capture.py         reader_rapidocr.py           aggregator.py           notifier.py
                                                     orchestrated by monitor.py
```

| Module | Role |
|--------|------|
| `score_reader.py` | `ScoreReader` ABC + `ScoreResult` dataclass. The swap point — an API-based reader could implement the same interface. |
| `reader_rapidocr.py` | RapidOCR implementation. ROI crop → OCR tokens → parse. |
| `capture.py` | `grab_frame` (one-shot) + `StreamGrabber` (persistent). |
| `hdhomerun.py` | Lineup discovery, channel→URL resolution (stdlib urllib). |
| `aggregator.py` | `ScoreTracker` temporal state machine + `format_event`. |
| `notifier.py` | Discord webhook POST (stdlib). |
| `monitor.py` | CLI, `.env` load, IDLE/ACTIVE polling loop, `--analyze`/`--test`. |

## Key decisions & why

- **RapidOCR, not tesseract.** Bake-off on real frames: tesseract returned
  garbage on the stylized mixed-polarity scorebug; RapidOCR read it cleanly with
  bounding boxes + confidence. tesseract 4.1.1 *is* apt-installed but unused.
- **No color/position hardcoding.** An earlier teal-color + fixed-pixel approach
  was rejected (must generalize across matches). The parser keys on structure:
  two team codes with digits between them in a top-left ROI. Works for any teams.
- **Parse strategy** (`_parse`): score digits are the pure-numeric tokens; team
  codes are the outermost alphabetic tokens *outside* the digit span (so the
  central FIFA "26" trophy logo — whether read as digits `26` or letters like
  `FIF`/`TIF` — is ignored). Home = leftmost digit, away = rightmost.
- **`min_conf=0.8`.** Real reads sit at 0.93–1.0; blips at 0.5–0.65. But some
  blips are *high*-confidence (see below), so conf alone is not enough.
- **Both-teams trust gate** (in `ScoreTracker`, the important one). Blips like
  `9-0 ?-ESP` at conf 0.88 happen when the scorebug **animates in/out** and
  renders partially — one team code missing + a misread digit. The "9" misread
  *recurs*, so it can appear twice and defeat confirm-K. Fix: a stable scorebug
  always shows **both** team codes, so ignore any read with a missing team.

## The scorebug

Fixed top-left graphic, layout:
`[clock] [flagL] HOME <h> (FIFA trophy) <a> AWAY [flagR]`.
Digits are dark on a teal background (NOT white — an early wrong assumption).
Also appears during **halftime highlights** showing the *old* score + *old*
clock — hence the monotonic rule.

## Capture / ffmpeg gotchas

- **Fast open:** `-probesize 500000 -analyzeduration 0 -fflags nobuffer`. Default
  analyzeduration (~5 s) made each open ~9 s; this cuts it to ~2 s.
- **Gray frames:** with the fast open, the HEVC decoder emits ~1.5 s of
  error-concealed **gray** frames before it syncs to a keyframe. `grab_frame`
  uses `seek=2.0` to skip them. seek ≤1.0 → gray frames → reader sees "no
  scorebug". Don't lower it.
- **`StreamGrabber`** (ACTIVE mode): one persistent ffmpeg → `image2pipe`/png; a
  background thread reads whole PNGs (`_read_png` parses length/type/CRC to IEND)
  and keeps only the **newest** (discards backlog → no lag). `read(after_seq=)`
  blocks for a fresh frame. Verified: match clock tracks wall clock 1:1, no drift.
- **No tuner leak:** `StreamGrabber`'s ffmpeg self-terminates when its stdout
  pipe breaks (parent dies); `finally: grabber.stop()` covers Ctrl-C.
- **Test-frame extraction** from the 20 GB `.ts` recordings uses input seeking
  (`ffmpeg -ss <t> -i file`) — reads only a chunk near `t`, ~2 s over WiFi.
  `.ts` recordings are H.264 1080p50; the live stream is HEVC.

## Measured performance

OCR ~1–2.5 s/frame (CPU). One-shot grab ~4 s. Persistent stream: ~3 s tune-in
once, then ~2 s/cycle (OCR-bound). Score accuracy: 100% across 5 test matches
(NED-JPN/SWE/MAR, CAN-MAR, PAR-FRA), zero false digits pre-blip-fix.

## HDHomeRun

FLEX QUATRO `HDFX-4DT` @ **10.43.70.192**, 4 tuners, DVB-T antenna, HEVC 1080p.
Endpoints: `/discover.json`, `/lineup.json`, `/lineup_status.json`, `/guide.json`
(EPG). Stream URL: `http://<ip>:5004/auto/v<GuideNumber>`.
**NOS FIFA WK airs on NPO 1 = channel 1 = `.../auto/v1`.** (NPO 2/3 = news/drama.)

## Aggregation logic (`ScoreTracker`)

`update(ScoreResult) -> ScoreEvent | None`. Order: reject if not present / no
score → reject if a team code missing → new-match auto-reset (teams changed AND
total dropped) → monotonic guard (total < confirmed → ignore) → confirm candidate
over `confirm_k` (default 2) identical reads → emit. First confirmed score emits
as a "start" event (`prev is None`, `▶️`). `reset()` clears state (monitor calls
it on the 30-min match-end timeout). Scorer = whichever side's count rose.

## Dev workflow

- **Dry-run OCR** on local PNGs (no HDHR): `python3 monitor.py --analyze *.png`.
- **Discord test:** `python3 monitor.py --test`.
- **One live grab:** `--once` (+ `--verbose` to see the read; reads log at DEBUG).
- No test suite; validation is ad-hoc scripts against extracted frames.
- Editing files does NOT affect an already-running monitor (no auto-reload).

## Known limitations

- **VAR/downward corrections** are ignored (monotonic rule) — accepted trade vs
  the far-more-common halftime replays.
- **Team codes are best-effort** display text; e.g. Canada's possession dot makes
  `CAN` read as `OCA`. Never affects the score (digits are separate).
- Assumes the NOS/FIFA-WC scorebug style; a different broadcaster would need the
  parser revisited (but not the color, since it's structural).

## Environment

Python 3.10, WSL2 Linux. Deps: `pillow numpy rapidocr-onnxruntime` (pip);
`ffmpeg`/`ffprobe` on PATH (apt). `numpy` was pre-installed. `.env` holds
`DISCORD_WEBHOOK_URL` (gitignored — the current webhook was pasted in plaintext
during dev, treat as compromised if repo goes public). Recordings live at
`/mnt/x/tvrecordings/NOS FIFA WK Voetbal 2026 (2026)/Season 2026/` (network disk).

## Future: EPG gating (parked)

Goal: when no football is scheduled, skip OCR entirely and **release the tuner**.
`/guide.json` gives per-channel programme listings with `StartTime`/`EndTime`
(epoch) and titles — filter NPO 1 for "NOS"/"WK"/"Voetbal". Design leaves a clean
seam: gate the IDLE→ACTIVE decision (and whether to poll at all) on "is football
on now". Until then the IDLE 1-min baseline always runs.
