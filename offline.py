"""Offline goal extraction from a pre-recorded `.ts` stream.

Given a recording of a match, find every score advance (goal) with as few OCR
calls as possible and write a re-encoded replay clip per goal to a local folder
(no Discord). It exploits that the *true* score is a monotonic staircase in match
time, but the *read* score is not (halftime highlights re-show old, lower scores;
the scorebug is often absent during replays). So:

  Phase 1 — coarse grid. Probe the score on a uniform grid (one probe per
  `grid_seconds`). Each probe reads K frames near a timestamp and takes the
  best-supported valid read (both team codes present). Rising totals that
  *persist* across grid points become confirmed goals; replay dips fall below the
  running max and are ignored (same monotonic idea as the live ScoreTracker). A
  lone high blip is rejected by the persistence requirement.

  Phase 2 — binary-search refine. Within each grid interval where the total rose,
  binary-search for the transition instant (per goal, so a multi-goal interval is
  split). Inside one ~grid_seconds window the score is locally monotonic, so
  binary search is safe there and pins each goal to ~`precision` seconds.

Cost: ~duration/grid_seconds grid probes + ~log2(grid_seconds/precision) per goal
— tens of OCR calls per match instead of tens of thousands for a full scan.
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from aggregator import ScoreEvent
from capture import grab_frames_at, probe_duration
from clipper import auto_bitrates, encode_range, save_clip
from teams import canonical_code, name as team_name

log = logging.getLogger("offline")


@dataclass
class ProbeRead:
    score: tuple[int, int]
    home_team: str
    away_team: str
    clock: Optional[str]
    support: int          # frames (of the probe) that agreed on this score
    valid: int            # frames with any usable read

    @property
    def total(self) -> int:
        return self.score[0] + self.score[1]


def probe(path: str, reader, t: float, count: int, fps: float) -> Optional[ProbeRead]:
    """Read the live score near `t`. Grabs `count` frames spread over a short
    window, keeps reads with a score *and both team codes* (a stable scorebug
    always shows both — a missing one is a mid-transition frame), and returns the
    most-supported score (tie-break: higher total). None if nothing is readable
    (scorebug absent / replay-only window).
    """
    try:
        frames = grab_frames_at(path, t, count=count, fps=fps)
    except Exception as e:
        log.debug("probe grab failed at %.1fs: %s", t, e)
        return None
    reads = []
    for f in frames:
        r = reader.read(f)
        if not r.present or r.score is None or not (r.home_team and r.away_team):
            continue
        reads.append((r.score, canonical_code(r.home_team),
                      canonical_code(r.away_team), r.clock))
    if not reads:
        return None
    counts = Counter(s for s, _, _, _ in reads)
    top = max(counts.items(), key=lambda kv: (kv[1], kv[0][0] + kv[0][1]))
    best_score = top[0]
    _, ht, at, clock = next(r for r in reads if r[0] == best_score)
    return ProbeRead(best_score, ht, at, clock, support=top[1], valid=len(reads))


@dataclass
class _Interval:
    t_lo: float           # last grid time still at the old total
    t_hi: float           # first grid time at the new total
    from_total: int
    to_total: int
    prev_score: tuple[int, int]
    prev_teams: tuple[str, str]
    read_hi: ProbeRead    # a read at the new level (teams/clock fallback)


def _persists(probes: list[Optional[ProbeRead]], idx: int, total: int,
              min_persist: int) -> bool:
    """True if the rise to `total` looks real rather than a one-off blip.

    Normally requires `total` (or higher) to show up in at least `min_persist`
    valid grid probes from `idx` onward — a lone high spike surrounded by lower
    reads is rejected. But the *final* goal of a match is legitimately fragile:
    the scorebug vanishes into studio analysis soon after, so it may appear in
    only one probe with nothing but `None`s behind it. So also accept when fewer
    than `min_persist` valid probes remain at all — we can't require more
    corroboration than exists, and near the end the only way to read a *higher*
    total is a genuine goal (replays read lower, below the running max).
    """
    valid_ahead = [p for p in probes[idx:] if p is not None]
    if sum(1 for p in valid_ahead if p.total >= total) >= min_persist:
        return True
    return len(valid_ahead) < min_persist


def find_goal_intervals(probes: list[tuple[float, Optional[ProbeRead]]],
                        min_persist: int) -> list[_Interval]:
    """From the grid probes, bound each goal to a [t_lo, t_hi] window using a
    persistent running-max staircase."""
    intervals: list[_Interval] = []
    confirmed_total = 0
    prev_score = (0, 0)
    prev_teams = ("", "")
    last_at_confirmed = probes[0][0] if probes else 0.0
    only_reads = [p for _, p in probes]
    for i, (t, p) in enumerate(probes):
        if p is None:
            continue
        if p.total > confirmed_total and _persists(only_reads, i, p.total, min_persist):
            intervals.append(_Interval(
                t_lo=last_at_confirmed, t_hi=t,
                from_total=confirmed_total, to_total=p.total,
                prev_score=prev_score,
                prev_teams=prev_teams or (p.home_team, p.away_team),
                read_hi=p))
            confirmed_total = p.total
            prev_score = p.score
            prev_teams = (p.home_team, p.away_team)
            last_at_confirmed = t
        elif p.total == confirmed_total:
            last_at_confirmed = t
            if confirmed_total > 0:
                prev_score = p.score
                prev_teams = (p.home_team, p.away_team)
    return intervals


def _nearest_readable(path: str, reader, center: float, lo_b: float, hi_b: float,
                      count: int, fps: float) -> Optional[tuple[float, ProbeRead]]:
    """Nearest readable probe to `center`, staying inside (lo_b, hi_b), searching
    outward with *exponentially* growing offsets. A goal celebration animates the
    scorebug out and halftime blacks it for ~15 min; exponential reach crosses
    even that in ~8 probes (linear reach can't, and bailing there is what pinned a
    clip to the coarse grid bound). Returns (time, read) or None if the whole
    interval is blank."""
    d = 2.0
    while True:
        for t in (center - d, center + d):        # nearer side (left) first
            if lo_b < t < hi_b:
                p = probe(path, reader, t, count, fps)
                if p is not None:
                    return t, p
        if center - d <= lo_b and center + d >= hi_b:
            return None
        d *= 2.0


def _search_boundary(path: str, reader, lo: float, hi: float, target: int,
                     count: int, fps: float, precision: float) -> tuple[float, Optional[ProbeRead]]:
    """Smallest time in (lo, hi] whose total >= `target`, by binary search.

    Invariant from Phase 1: total(lo) < target <= total(hi), and both bounds are
    readable. A blacked-out midpoint (celebration / replay / halftime) is snapped
    to the nearest readable frame rather than aborting the search — the old bug
    was bailing on `None` and returning the coarse grid bound `hi`, so every clip
    landed up to a whole grid step late. Converges to ~`precision`: the first
    instant the new score is on screen, i.e. the same anchor the live path uses.
    """
    hi_read: Optional[ProbeRead] = None
    while hi - lo > precision:
        mid = (lo + hi) / 2.0
        p = probe(path, reader, mid, count, fps)
        if p is None:
            snapped = _nearest_readable(path, reader, mid, lo, hi, count, fps)
            if snapped is None:  # interval is entirely blank: can't narrow
                break
            mid, p = snapped
        if p.total >= target:
            hi, hi_read = mid, p
        else:
            lo = mid
    if hi_read is None:
        hi_read = probe(path, reader, hi, count, fps)
    return hi, hi_read


def find_goals(path: str, reader, *, grid_seconds: float = 120.0,
               probe_frames: int = 3, probe_fps: float = 2.0,
               min_persist: int = 2, precision: float = 2.0,
               start: float = 0.0, duration: Optional[float] = None
               ) -> list[tuple[float, ScoreEvent]]:
    """Return [(goal_time_s, ScoreEvent)] for every score advance in `path`."""
    if duration is None:
        duration = probe_duration(path)
    if not duration:
        raise RuntimeError(f"could not determine duration of {path}")

    grid = []
    t = start
    while t < duration:
        grid.append(t)
        t += grid_seconds
    log.info("Phase 1: %d grid probes over %.0f min (every %.0fs, %d frames each)",
             len(grid), duration / 60.0, grid_seconds, probe_frames)
    probes: list[tuple[float, Optional[ProbeRead]]] = []
    for gt in grid:
        p = probe(path, reader, gt, probe_frames, probe_fps)
        probes.append((gt, p))
        if p is not None:
            log.debug("  %6.0fs  %s %d-%d %s (support %d/%d)", gt, p.home_team,
                      p.score[0], p.score[1], p.away_team, p.support, p.valid)

    intervals = find_goal_intervals(probes, min_persist)
    total_goals = sum(iv.to_total - iv.from_total for iv in intervals)
    log.info("Phase 1 found %d goal(s) across %d interval(s)", total_goals, len(intervals))

    goals: list[tuple[float, ScoreEvent]] = []
    for iv in intervals:
        lo, hi = iv.t_lo, iv.t_hi
        prev = iv.prev_score
        prev_teams = iv.prev_teams
        for target in range(iv.from_total + 1, iv.to_total + 1):
            gt, read = _search_boundary(path, reader, lo, hi, target,
                                        probe_frames, probe_fps, precision)
            read = read or iv.read_hi
            score = read.score
            # Guard: within a multi-goal interval a boundary read should land on
            # this target's total; if OCR undershot, synthesize from prev + a
            # one-goal bump on the side the teams suggest (rare fallback).
            if score[0] + score[1] != target:
                score = (prev[0] + 1, prev[1]) if target > prev[0] + prev[1] else score
            ht = read.home_team or prev_teams[0]
            at = read.away_team or prev_teams[1]
            event = ScoreEvent(home=score[0], away=score[1], prev=prev,
                               home_team=ht, away_team=at, clock=read.clock)
            goals.append((gt, event))
            log.info("  goal @ %6.0fs (%s)  %s %d-%d %s",
                     gt, event.clock or "?:??", team_name(ht),
                     score[0], score[1], team_name(at))
            prev = score
            prev_teams = (ht, at)
            lo = gt  # the next goal in this interval is strictly later
    return goals


def extract_goals(path: str, reader, out_dir: str, *,
                  grid_seconds: float = 120.0, probe_frames: int = 3,
                  probe_fps: float = 2.0, min_persist: int = 2,
                  precision: float = 2.0, pre: float = 25.0, post: float = 5.0,
                  height: int = 720, vbitrate: Optional[str] = None,
                  max_bytes: int = 10 * 1024 * 1024, nvenc: bool = False) -> int:
    """Find every goal in `path` and write a replay clip per goal to `out_dir`.
    Returns the number of clips written. `pre`/`post` frame the clip around the
    detected goal instant (random access — no ring buffer needed)."""
    goals = find_goals(path, reader, grid_seconds=grid_seconds,
                       probe_frames=probe_frames, probe_fps=probe_fps,
                       min_persist=min_persist, precision=precision)
    if not goals:
        log.info("no goals found in %s", os.path.basename(path))
        return 0
    os.makedirs(out_dir, exist_ok=True)
    try:
        when = _dt.datetime.fromtimestamp(os.path.getmtime(path))
    except OSError:
        when = None
    dur = pre + post
    written = 0
    for idx, (gt, event) in enumerate(goals, 1):
        start = max(0.0, gt - pre)
        tmpdir = tempfile.mkdtemp(prefix="offline-clip-")
        out_mp4 = os.path.join(tmpdir, "clip.mp4")
        try:
            if vbitrate:
                ok = encode_range(path, start, dur, out_mp4, height=height,
                                  vbitrate=vbitrate, nvenc=nvenc)
            else:
                vb, mr, bs = auto_bitrates(max_bytes, dur)
                ok = encode_range(path, start, dur, out_mp4, height=height,
                                  vbitrate=vb, maxrate=mr, bufsize=bs, nvenc=nvenc)
            if not ok:
                log.warning("goal %d: encode failed, skipping", idx)
                continue
            saved = save_clip(out_dir, out_mp4, event, clock=event.clock,
                              when=when, tag=event.clock or f"{idx:02d}")
            if saved:
                written += 1
                log.info("saved %s (%.1f MB)", os.path.basename(saved),
                         os.path.getsize(saved) / 1e6)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    log.info("wrote %d/%d goal clip(s) to %s", written, len(goals), out_dir)
    return written
