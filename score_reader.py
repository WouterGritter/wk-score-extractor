"""Score-reading interface: a `ScoreReader` maps a frame to a `ScoreResult`.

Implementations (RapidOCR today, e.g. an API-based one later) are interchangeable.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Optional, Union

import numpy as np
from PIL import Image

# A frame may be a file path, a PIL image, or an HxWx3 RGB array.
Frame = Union[str, Image.Image, np.ndarray]


def to_rgb_array(frame: Frame) -> np.ndarray:
    if isinstance(frame, str):
        return np.asarray(Image.open(frame).convert("RGB"))
    if isinstance(frame, Image.Image):
        return np.asarray(frame.convert("RGB"))
    arr = np.asarray(frame)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[2] == 4:
        arr = arr[:, :, :3]
    return arr


@dataclass
class ScoreResult:
    present: bool                         # scorebug detected on screen
    home: Optional[int] = None
    away: Optional[int] = None
    home_team: Optional[str] = None       # e.g. "NED", best effort
    away_team: Optional[str] = None
    clock: Optional[str] = None           # e.g. "51:50", best effort
    confidence: float = 0.0               # 0..1 in the score digits
    raw: object = field(default=None, repr=False)

    @property
    def score(self) -> Optional[tuple[int, int]]:
        """(home, away) if both digits were read, else None."""
        if self.present and self.home is not None and self.away is not None:
            return (self.home, self.away)
        return None

    def __str__(self) -> str:
        if not self.present:
            return "<no scorebug>"
        h = "?" if self.home is None else self.home
        a = "?" if self.away is None else self.away
        teams = ""
        if self.home_team or self.away_team:
            teams = f" {self.home_team or '?'}-{self.away_team or '?'}"
        clk = f" @{self.clock}" if self.clock else ""
        return f"{h}-{a}{teams}{clk} (conf {self.confidence:.2f})"


class ScoreReader(abc.ABC):
    @abc.abstractmethod
    def read(self, frame: Frame) -> ScoreResult:
        ...

    def __call__(self, frame: Frame) -> ScoreResult:
        return self.read(frame)
