"""RapidOCR score reader.

Crops a top-left ROI, runs RapidOCR, then reads the scorebug row: the two score
digits are the pure-numeric tokens; the team codes are the outermost alphabetic
tokens (so the central FIFA trophy / "26" logo is ignored). Adapts to any teams.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np
from PIL import Image

from score_reader import Frame, ScoreReader, ScoreResult, to_rgb_array

# Top-left ROI as a fraction of the frame (x0, y0, x1, y1).
DEFAULT_ROI_FRAC = (0.0, 0.0, 0.38, 0.11)

_CLOCK_RE = re.compile(r"^\s*\d{1,3}\s*[:.'`]\s*\d{2}\s*$")
# Look like team codes but never are (trophy / channel logos).
_NOT_TEAM = {"FIFA", "HFA", "NOS", "LIVE", "FFA", "HHA"}


@dataclass
class Token:
    x0: float
    x1: float
    y0: float
    y1: float
    text: str
    conf: float

    @property
    def xc(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _alpha_code(text: str) -> str:
    return re.sub(r"[^A-Z]", "", text.upper())


class RapidOcrScoreReader(ScoreReader):
    def __init__(self, roi_frac=DEFAULT_ROI_FRAC, upscale: int = 3,
                 min_conf: float = 0.8):
        self.roi_frac = roi_frac
        self.upscale = upscale
        self.min_conf = min_conf
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:  # lazy: loading models is slow
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR()
        return self._engine

    def _ocr_tokens(self, rgb: np.ndarray) -> list[Token]:
        h, w = rgb.shape[:2]
        fx0, fy0, fx1, fy1 = self.roi_frac
        x0, y0, x1, y1 = int(fx0 * w), int(fy0 * h), int(fx1 * w), int(fy1 * h)
        roi = Image.fromarray(rgb[y0:y1, x0:x1])
        if self.upscale != 1:
            roi = roi.resize((roi.width * self.upscale, roi.height * self.upscale),
                             Image.LANCZOS)
        result, _ = self.engine(np.asarray(roi))
        tokens: list[Token] = []
        if result:
            for box, text, conf in result:
                xs = [p[0] for p in box]
                ys = [p[1] for p in box]
                tokens.append(Token(min(xs), max(xs), min(ys), max(ys),
                                    text.strip(), float(conf)))
        tokens.sort(key=lambda t: t.x0)
        return tokens

    def _parse(self, tokens: list[Token]) -> ScoreResult:
        if not tokens:
            return ScoreResult(present=False, raw=tokens)

        clock_tok = next((t for t in tokens if _CLOCK_RE.match(t.text)), None)

        # Scorebug row band, anchored on the clock (or the top token cluster).
        if clock_tok is not None:
            cy, ch = clock_tok.yc, max(clock_tok.h, 1)
        else:
            top = min(tokens, key=lambda t: t.yc)
            cy, ch = top.yc, max(top.h, 1)
        band = [t for t in tokens if abs(t.yc - cy) <= 1.2 * ch and t is not clock_tok]

        # Score digits: 1-2 digit numeric tokens. "26" is the trophy logo, not a score.
        score_digits = [t for t in band
                        if _digits(t.text) and len(_alpha_code(t.text)) == 0
                        and len(_digits(t.text)) <= 2 and _digits(t.text) != "26"]
        score_digits.sort(key=lambda t: t.xc)

        # Team codes sit OUTSIDE the digits; the trophy (if misread as letters) is
        # between them, so restricting to outside excludes it.
        alpha = [t for t in band
                 if 2 <= len(_alpha_code(t.text)) <= 4
                 and _alpha_code(t.text) not in _NOT_TEAM]
        home_team = away_team = None
        if score_digits:
            lo, hi = score_digits[0].xc, score_digits[-1].xc
            left = [a for a in alpha if a.xc < lo]
            right = [a for a in alpha if a.xc > hi]
            if left:
                home_team = _alpha_code(max(left, key=lambda a: a.xc).text)[:3]
            if right:
                away_team = _alpha_code(min(right, key=lambda a: a.xc).text)[:3]

        home = away = None
        hc = ac = 0.0
        if len(score_digits) >= 2:
            home = int(_digits(score_digits[0].text)); hc = score_digits[0].conf
            away = int(_digits(score_digits[-1].text)); ac = score_digits[-1].conf
        elif len(score_digits) == 1:
            # One digit: assign to the side it sits on relative to the team codes.
            d = score_digits[0]
            lefts = [a for a in alpha if a.xc < d.xc]
            rights = [a for a in alpha if a.xc > d.xc]
            if rights and not lefts:
                away = int(_digits(d.text)); ac = d.conf
            else:
                home = int(_digits(d.text)); hc = d.conf

        present = (clock_tok is not None) or (len(alpha) >= 2 and len(score_digits) >= 1)

        conf = min(hc, ac) if (home is not None and away is not None) else max(hc, ac, 0.0)
        if hc < self.min_conf:
            home = None
        if ac < self.min_conf:
            away = None

        return ScoreResult(
            present=present, home=home, away=away,
            home_team=home_team, away_team=away_team,
            clock=clock_tok.text if clock_tok else None,
            confidence=conf, raw=tokens,
        )

    def read(self, frame: Frame) -> ScoreResult:
        return self._parse(self._ocr_tokens(to_rgb_array(frame)))


if __name__ == "__main__":
    import sys, os
    reader = RapidOcrScoreReader()
    for path in sys.argv[1:]:
        print(f"{os.path.basename(path):28s} {reader.read(path)}")
