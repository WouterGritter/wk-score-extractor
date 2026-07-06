"""Turn noisy per-frame `ScoreResult`s into confirmed score changes.

Rules: confirm a new score over `confirm_k` reads; stay monotonic within a match
(a lower total is a highlight replay -> ignore); require both team codes (a
missing one means a mid-transition frame with unreliable digits); emit the first
confirmed score too (as a "start" event). `reset()` clears state between matches.

Note: the monotonic rule also ignores legitimate downward corrections (e.g. VAR),
but halftime replays are far more common, so we optimise for those.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from score_reader import ScoreResult


@dataclass
class ScoreEvent:
    home: int
    away: int
    prev: Optional[tuple[int, int]]      # None => first sighting of the match
    home_team: Optional[str]
    away_team: Optional[str]
    clock: Optional[str]

    @property
    def is_first(self) -> bool:
        return self.prev is None

    @property
    def scorer(self) -> Optional[str]:
        """Team that just scored ('home'/'away' side name), if determinable."""
        if self.prev is None:
            return None
        if self.home > self.prev[0]:
            return self.home_team or "home"
        if self.away > self.prev[1]:
            return self.away_team or "away"
        return None


def format_event(e: ScoreEvent) -> str:
    ht = e.home_team or "Home"
    at = e.away_team or "Away"
    line = f"{ht} {e.home}-{e.away} {at}"
    clock = f"  ({e.clock})" if e.clock else ""
    if e.is_first:
        return f"▶️  {line}{clock}"
    scorer = e.scorer
    if scorer:
        return f"⚽  GOAL {scorer}!  {line}{clock}"
    return f"⚽  GOAL!  {line}{clock}"


class ScoreTracker:
    def __init__(self, confirm_k: int = 2, require_both_teams: bool = True):
        self.confirm_k = max(1, confirm_k)
        self.require_both_teams = require_both_teams
        self.confirmed: Optional[tuple[int, int]] = None
        self.match_teams: Optional[tuple[Optional[str], Optional[str]]] = None
        self.home_team: Optional[str] = None
        self.away_team: Optional[str] = None
        self._candidate: Optional[tuple[int, int]] = None
        self._candidate_count = 0

    def reset(self) -> None:
        self.confirmed = None
        self.match_teams = None
        self.home_team = None
        self.away_team = None
        self._candidate = None
        self._candidate_count = 0

    def _remember_teams(self, r: ScoreResult) -> None:
        if r.home_team:
            self.home_team = r.home_team
        if r.away_team:
            self.away_team = r.away_team

    def update(self, r: ScoreResult) -> Optional[ScoreEvent]:
        """Feed one frame's result; return a ScoreEvent on a confirmed change."""
        if not r.present or r.score is None:
            return None
        # A stable scorebug shows both team codes; a missing one means a
        # mid-transition frame whose digits aren't trustworthy (e.g. "9-0 ?-ESP").
        if self.require_both_teams and not (r.home_team and r.away_team):
            return None
        self._remember_teams(r)
        s = r.score
        total = s[0] + s[1]

        # New-match detection: teams changed and the total dropped.
        if (self.confirmed is not None and r.home_team and r.away_team
                and self.match_teams is not None
                and (r.home_team, r.away_team) != self.match_teams
                and total < self.confirmed[0] + self.confirmed[1]):
            self.reset()
            self._remember_teams(r)

        # Monotonic guard: ignore replays/highlights showing a lower total.
        if self.confirmed is not None and total < self.confirmed[0] + self.confirmed[1]:
            return None

        if s == self.confirmed:
            self._candidate = None
            self._candidate_count = 0
            return None

        # Accumulate confirmation for a candidate new score.
        if s == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = s
            self._candidate_count = 1

        if self._candidate_count >= self.confirm_k:
            prev = self.confirmed
            self.confirmed = s
            if self.match_teams is None:
                self.match_teams = (self.home_team, self.away_team)
            self._candidate = None
            self._candidate_count = 0
            return ScoreEvent(
                home=s[0], away=s[1], prev=prev,
                home_team=self.home_team, away_team=self.away_team,
                clock=r.clock)
        return None
