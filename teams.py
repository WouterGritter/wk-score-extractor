"""Team-code canonicalisation + display names for the 2026 World Cup.

The OCR reads a 3-letter FIFA code off the scorebug, but stylised glyphs blip
(e.g. `USA`->`OSA` ~40% of the time, `CAN`->`OCA` from Canada's possession dot).
`canonical_code` snaps a noisy read back to a known code via Levenshtein distance
(threshold 2), which both de-noises the display *and* stabilises the tracker's
confirm-K signature (an `OSA`/`USA` flip would otherwise never confirm). `name`
maps a code to a full country name for the Discord message.

`teams.json` is the source of truth: {FIFA code -> country name}, all 48 teams.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_TEAMS_PATH = Path(__file__).with_name("teams.json")

# code -> full country name (e.g. "USA" -> "United States").
NAMES: dict[str, str] = json.loads(_TEAMS_PATH.read_text(encoding="utf-8"))
CODES: tuple[str, ...] = tuple(NAMES)

# Max edit distance for snapping an unknown read to a known code.
MAX_DISTANCE = 2


def _clean(raw: str) -> str:
    return "".join(c for c in raw.upper() if c.isalpha())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # deletion
                           cur[j - 1] + 1,       # insertion
                           prev[j - 1] + (ca != cb)))  # substitution
        prev = cur
    return prev[-1]


def canonical_code(raw: Optional[str]) -> Optional[str]:
    """Snap a noisy OCR team code to a known FIFA code.

    Exact matches pass straight through; otherwise the nearest known code within
    `MAX_DISTANCE` wins (first-listed on a tie). Unknown reads that are too far
    from any code are returned cleaned but unchanged (best-effort display text).
    """
    if not raw:
        return raw
    code = _clean(raw)
    if not code or code in NAMES:
        return code or raw
    best, best_dist = code, MAX_DISTANCE + 1
    for known in CODES:
        d = _levenshtein(code, known)
        if d < best_dist:
            best, best_dist = known, d
    return best if best_dist <= MAX_DISTANCE else code


def name(code: Optional[str]) -> Optional[str]:
    """Full country name for a code, falling back to the code itself."""
    if not code:
        return code
    return NAMES.get(code, code)
