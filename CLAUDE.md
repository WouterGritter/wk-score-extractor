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
| `teams.py` / `teams.json` | Canonicalise noisy OCR codes (Levenshtein) + code→full-name map (all 48 WC 2026 teams). |
| `notifier.py` | Discord webhook POST + multipart file upload (stdlib). |
| `clipper.py` | Cut a goal replay (ring buffer or file range) → re-encode → upload/save. |
| `offline.py` | Offline goal search on a recorded `.ts`: grid scan + binary-search refine → clip per goal. |
| `monitor.py` | CLI, `.env` load, IDLE/ACTIVE polling loop, `--analyze`/`--test`/`--offline`. |

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
- **Confirm on the full signature, not just the score.** A goal fires a scorebug
  **animation** that keeps the score right but briefly *misreads a present team
  code* (`USA`→`OUS`, `USA`→`OUS`… → the `USA 0-1 BEL` goal posted as `OUS 0-1
  BEL`). So the confirm-K candidate is `(score, home_team, away_team)`: it only
  advances while all three are stable, and the emitted event uses the *stable*
  candidate's teams — not a per-frame latch (that latch was how the misread
  leaked). Teams are set on the tracker only at confirmation. Residual edge: a
  team misread that *recurs* for K consecutive frames still commits (same class as
  the recurring-"9" digit blip); lever is a higher `confirm_k`, left at 2.

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
- **Team codes are canonicalised, then best-effort.** `ScoreTracker.update`
  snaps each raw OCR code to the nearest known FIFA code within Levenshtein
  distance 2 (`teams.canonical_code`) *before* it enters the confirm signature —
  so `USA`→`OSA` blips (~40%) and `CAN`→`OCA` (possession dot) both resolve to the
  right team, and a flip like `USA`/`OSA` no longer defeats confirm-K. `format_event`
  and the clip caption then map the code to a full name (`teams.json`, e.g. `USA`→
  "United States"). Gotcha: nearest-neighbour on a dense 3-letter space can mis-snap
  a *stray* read (e.g. `OUS`→`AUS`, closer than `USA`), but that only reaches the
  message if it's the **stable/confirmed** read — a transient mis-snap just fails to
  accumulate confirm-K. Never affects the score (digits are separate).
- Assumes the NOS/FIFA-WC scorebug style; a different broadcaster would need the
  parser revisited (but not the color, since it's structural).

## Environment

Python 3.10, WSL2 Linux. Deps: `pillow numpy rapidocr-onnxruntime` (pip);
`ffmpeg`/`ffprobe` on PATH (apt). `numpy` was pre-installed. `.env` holds
`DISCORD_WEBHOOK_URL` (gitignored — the current webhook was pasted in plaintext
during dev, treat as compromised if repo goes public). Recordings live at
`/mnt/x/tvrecordings/NOS FIFA WK Voetbal 2026 (2026)/Season 2026/` (network disk).

## GPU / CUDA (prod on the Tesla P4 host)

OCR is the ACTIVE-loop bottleneck (~1–2.5 s/frame on CPU); the GPU path exists to
cut that. **Code is provider-agnostic:** `RapidOcrScoreReader(use_cuda=...)` just
sets `det/cls/rec_use_cuda` on RapidOCR, which routes onnxruntime to
`CUDAExecutionProvider`. `monitor.py` turns it on via `--cuda` **or** `USE_CUDA=1`
(env/.env). Outside Docker the code default is **CPU**; the default Docker image
now bakes `USE_CUDA=1` (GPU is the default deployment — see below). The CPU dev
flow (`--analyze`, `--once`, etc.) is untouched.

- **The real work is packaging, not code.** `rapidocr-onnxruntime` bundles CPU
  `onnxruntime`; the GPU image must `pip uninstall onnxruntime` then install
  `onnxruntime-gpu` (having both → Python imports the CPU one and silently falls
  back — RapidOCR only logs a warning, doesn't error). Done in the default `Dockerfile`.
- **Default image is GPU.** `Dockerfile` (base `nvidia/cuda:12.4.1-cudnn-runtime`
  = CUDA 12 + cuDNN 9, what `onnxruntime-gpu==1.23.2` links against) +
  `docker-compose.yml` (GPU reservation, `USE_CUDA=1`). Deploy: `docker compose up
  -d --build`. The **CPU-only** variant is `Dockerfile.cpu` + `docker-compose.cpu.yml`
  (plain `onnxruntime`): `docker compose -f docker-compose.cpu.yml up -d --build`.
- **P4 = Pascal (compute 6.1):** works with CUDA 12; host needs driver ≥ 525 and
  `nvidia-container-toolkit`. Verify with `nvidia-smi` on the host.
- **The GPU host must reach the HDHomeRun** (`${HDHR_IP}`) on the LAN — capture is
  unchanged, still pulls the stream directly.
- **First read after enabling CUDA is slow** (10–30 s: cuDNN autotune + models onto
  the GPU). One-time — the engine is a lazy singleton.

### CPU usage & the two levers (measured on the P4 VM, GPU OCR on)

`top` showed **ffmpeg ~138%** + **python ~98%** — a saturated 2-vCPU VM. Causes and
fixes:

- **ffmpeg ~138% = full-rate HEVC decode, not 4 fps.** `-vf fps=4` only throttles
  the *output*; ffmpeg still decodes every input frame (1080p @ 25–50 fps) before the
  filter drops them. This is the one place GPU decode genuinely helps (earlier "4 fps
  decode is trivial" was wrong — the *decode* isn't 4 fps). Fix: **NVDEC** via
  `--hwaccel` / `FFMPEG_HWACCEL=1` (`_hwaccel_args` prepends `-hwaccel cuda`).
  Opt-in/off-by-default because it needs a cuda-enabled ffmpeg build **and** the
  `video` driver capability (`NVIDIA_DRIVER_CAPABILITIES=all`, set in the default Dockerfile;
  compute-only won't expose libnvcuvid). Verify the build first:
  `docker run --rm --entrypoint ffmpeg <img> -hwaccels` (expect `cuda`).
- **python ~98% = OCR-ing 4 frames/sec with no throttle.** Even with GPU inference,
  per-frame CPU work remains (PNG decode, 3× upscale, RapidOCR det post-processing).
  The ACTIVE loop has no sleep, so it runs as fast as frames arrive. Fix (free, no
  rebuild): **lower `--fps` to 1–2.** With `confirm x2`, 1 fps confirms a score in
  ~2 s. This cuts python's rate ~4× and ffmpeg's PNG-encode share, but NOT ffmpeg's
  decode (that's what NVDEC is for). Do the fps change first, then NVDEC if needed.

## Goal-replay clips

On each confirmed goal, post a short replay video to Discord as a **follow-up
message** (the text alert fires first, unchanged; the video lands a bit later
after encode + upload). Off by default in code (`--clip` / `CLIP_ENABLE=1`); the
**compose files enable it** and mount the tmpfs it needs. Validated end-to-end on
real live goals (one tuner, real Discord upload). Default clip is ~30 s (25 s
before detection + 5 s post-roll) at 720p.

- **One tuner, no extra decode.** The ring is a **second output on the existing
  `StreamGrabber` ffmpeg**: `-map 0:v -map 0:a? -c copy -f segment` writes
  keyframe-aligned `.ts` segments (`-segment_wrap` bounds the footprint). It's a
  packet remux — near-zero CPU on top of the decode OCR already pays. The OCR
  output (`-an -vf fps=N` PNG pipe) is unchanged. The ring only exists while
  ACTIVE, which is exactly when goals happen.
- **Buffer wants audio; OCR doesn't.** That's why they're separate ffmpeg outputs
  (OCR path is `-an` + filtered fps; ring is full copy incl. `0:a?`). `0:a?` makes
  audio optional so a rare audio-less stream still yields a (silent) clip.
- **All in RAM.** `buffer_dir` is a tmpfs (`/buf`, mounted 512m by compose; bare
  `docker run` needs `--tmpfs /buf` or it falls back to overlayfs). `snapshot()`
  copies the chosen segments into a work dir **inside** `buffer_dir` so the copy +
  re-encode never touch disk. Answers the original "without hammering the disk":
  ring is copy-only ~1 MB/s in RAM, one ~9 MB mp4 leaves RAM per goal.
- **`snapshot(seconds)`** picks the most recent `ceil(seconds/segment_time)`
  segments by mtime, **drops the newest** (still being written by the muxer), and
  **copies** them out (not hardlink/reference — the muxer truncates-in-place on
  wrap, which would corrupt a shared inode). Ring holds `clip_seconds*3` so a slow
  encode can't race the wrap. Returns an ffmpeg concat-demuxer list.
- **Re-encode is mandatory, not just for size.** Discord only renders an inline
  player for **H.264/AAC mp4**; HEVC-in-mp4 usually won't preview. `clipper.encode_clip`
  downscales (`scale=-2:<CLIP_HEIGHT>`, default 720) + `-movflags +faststart`.
  `auto_bitrates` sizes the video bitrate to `CLIP_MAX_MB` so the clip fits a 10 MB
  cap (a short 30s clip leaves plenty of bitrate headroom) and a boosted server
  (25/100 MB) gets a proportionally sharper clip — no fixed guess to outgrow.
- **NVENC optional** (`CLIP_NVENC=1` → `h264_nvenc`): the P4 has an encoder and the
  `video` capability is already set, but libx264 (CPU) handles one short clip per goal
  fine, so it's off by default (an nvenc-less ffmpeg build would error).
- **Timing / post-roll.** Detection already lags the goal by seconds (scorebug
  update + confirm-K), and the goal action sits inside the trailing buffer
  (default 25s before detection). `CLIP_POSTROLL` (default 5s) waits *after*
  detection before cutting so the clip also catches the on-screen celebration.
- **Off the OCR loop.** The whole cut→encode→upload runs in a **daemon thread**
  per goal (spawned only for real goals, `not event.is_first`, when a grabber
  exists); it swallows all exceptions so a clip failure never touches the monitor.
- **Upload is stdlib multipart** (`DiscordNotifier.send_file`): a `payload_json`
  field + the file as `files[0]`. It skips (warns, doesn't send) if the file is
  over `max_bytes`, so a mis-sized clip can't 413.

## Offline extraction (`offline.py`)

`monitor.py --offline INPUT.ts` finds every goal in a recording and writes a clip
per goal to `--goals-dir` (default `./goals`) — no Discord, no HDHomeRun. The
point is **minimal OCR**: a full 4 fps scan of a 2.5 h match is tens of thousands
of reads; this does ~tens.

- **Why not naive binary search.** The *true* score is a monotonic staircase, but
  the *read* score is NOT globally monotonic: halftime highlights re-show old,
  lower scores, and the scorebug is absent during replays/studio. Pure recursive
  binary search prunes an interval when its endpoints read equal — a replay dip
  or a blank frame at an endpoint would silently drop a real goal. So it's a
  **hybrid**, and it leans on the same monotonic-running-max idea as `ScoreTracker`.
- **Phase 1 — coarse grid.** Probe once per `--grid-seconds` (default 120). Each
  probe (`probe()`) reads `--probe-frames` (default 3) frames spread over ~1.5 s
  via `capture.grab_frames_at` (input seeking, fast on a 30 GB file), keeps reads
  with a score **and both team codes** (canonicalised), and returns the
  most-supported score. A rising total that **persists** across probes becomes a
  confirmed goal; replays read *below* the running max and are ignored for free.
- **Phase 2 — binary-search refine.** For each grid interval where the total
  rose, `_search_boundary` binary-searches the goal instant *per goal* (a
  multi-goal interval is split by searching target totals `from+1..to`). Inside
  one ~grid_seconds window the score is locally monotonic, so this is safe; it
  pins each goal to `precision` (2 s) — the **first instant the new score is on
  screen, i.e. the same anchor the live path uses**, so the same `[−CLIP_SECONDS,
  +CLIP_POSTROLL]` (25/5) window catches the goal exactly as live does.
- **THE other gotcha — don't bail on an unreadable midpoint.** A goal celebration
  animates the scorebug *out* (and brief replays blank it), so a probe landing
  exactly on the transition often reads `None`. The first version *broke* out of
  the search on `None` and returned the interval's upper bound `hi` — which is a
  **coarse grid point**, up to a whole `grid_seconds` after the real update. Tell:
  every detected goal time came out as an exact multiple of the grid, and the
  clips sat 0–150 s *after* the goal (only the one goal whose midpoints were all
  readable refined correctly). Fix: `_resolve` reads a `None` midpoint via the
  nearest readable frame within ±`reach` (30 s) — a celebration gap or replay no
  longer aborts the search; it only gives up on a genuine long blackout (deep
  halftime), which can't contain a rise to a not-yet-scored total anyway.
- **THE gotcha — the final goal is fragile.** `_persists` normally requires a
  higher total to appear in ≥ `min_persist` (2) grid probes, to reject a one-off
  high blip. But the *last* goal of a match is legitimately seen once: the
  scorebug vanishes into studio analysis right after, so every later probe is
  `None`. First validation run on NED-SWE dropped the `4-1 → 5-1` because 5-1 hit
  only the last readable probe. Fix: `_persists` also accepts when **fewer than
  `min_persist` valid probes remain at all** — can't require more corroboration
  than exists, and near the end the only way to read a *higher* total is a real
  goal (replays read lower). Intermediate goals are unaffected: their successors
  read `>= total` (a later higher score still satisfies persistence), so only the
  terminal goal ever hits this branch.
- **Whole-probe misreads ≈ never**, which is why `min_persist` can be this loose:
  digits read ~100% and each probe already needs 3 frames to agree, so a probe
  reporting a spurious rise essentially doesn't happen. Team-code blips don't
  change the total.
- **Clips need no ring buffer** — the file has random access, so `clipper.encode_range`
  cuts `[goal − CLIP_SECONDS, goal + CLIP_POSTROLL]` straight from the source with
  input seeking + the same 720p/`auto_bitrates` path as live. Naming +
  save-to-disk is the shared `clipper.save_clip` (also used by the live path now).
- **Live now saves too.** `build_and_send_clip(..., save_dir=)` copies the encoded
  mp4 into `--goals-dir` before the temp dir is removed, in addition to the
  Discord upload — so live and offline share one local archive + naming scheme.
- **Cost knobs:** `--grid-seconds` (denser = safer, more OCR), `--probe-frames`
  (more = more robust per probe, more OCR), `--cuda` (GPU OCR). On CPU a probe is
  ~5-6 s, so a 2.5 h match at grid 150 is ~7-10 min end to end.

## Future: EPG gating (parked)

Goal: when no football is scheduled, skip OCR entirely and **release the tuner**.
`/guide.json` gives per-channel programme listings with `StartTime`/`EndTime`
(epoch) and titles — filter NPO 1 for "NOS"/"WK"/"Voetbal". Design leaves a clean
seam: gate the IDLE→ACTIVE decision (and whether to poll at all) on "is football
on now". Until then the IDLE 1-min baseline always runs.
