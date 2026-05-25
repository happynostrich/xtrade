"""Sentiment scorers for the news pipeline (Phase 5 / B1).

This module ships one in-tree baseline (`VaderScorer`) that needs **no
external dependency** — a small finance-flavoured lexicon plus a
VADER-style compound normalisation. The implementation is intentionally
simple so the Phase 5 burst can land without pulling new heavy deps
(NLTK / vaderSentiment / HuggingFace). Operators wanting higher-fidelity
baselines can register additional scorers via `register_scorer`.

Score range
-----------
`Scorer.score(text)` returns a float in ``[-1.0, 1.0]`` (compound):

  * `> 0` : net bullish
  * `< 0` : net bearish
  * `0`   : neutral / no lexicon hits / empty input

Determinism
-----------
All scorers in this module are pure functions of their input string —
the same text yields the same score, byte-for-byte. Tests rely on this.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


# A non-greedy word tokeniser: letters with optional apostrophes/hyphens.
# Numbers / punctuation are dropped (they carry no polarity in this baseline).
_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z'-]+")

# Window-size for negation inversion: a negator within this many tokens
# before a polarity word flips its sign.
_NEGATION_WINDOW = 3
_NEGATORS: frozenset[str] = frozenset(
    {"not", "no", "never", "nor", "n't", "nothing", "none", "without"}
)

# Hand-curated, finance / crypto-flavoured polarity lexicon. Scores in
# the canonical VADER range ``[-4, 4]`` before normalisation. Values are
# rounded to 1 decimal; ties are intentional. Keep this list small and
# readable — it is the spec, not a learned table.
_LEXICON: dict[str, float] = {
    # ----- bullish -----
    "bullish": 2.5, "bull": 2.0, "rally": 2.2, "rallies": 2.2, "rallied": 2.2,
    "surge": 2.5, "surges": 2.5, "surged": 2.5, "soar": 2.7, "soars": 2.7,
    "soared": 2.7, "rocket": 2.7, "moon": 2.4, "breakout": 2.0, "ath": 2.0,
    "gain": 1.5, "gains": 1.5, "rise": 1.3, "rises": 1.3, "rose": 1.3,
    "up": 0.8, "growth": 1.5, "profit": 2.0, "profits": 2.0, "win": 1.5,
    "wins": 1.5, "strong": 1.4, "outperform": 2.0, "upgrade": 1.7,
    "upgraded": 1.7, "buy": 1.3, "approved": 1.5, "approves": 1.5,
    "partnership": 1.2, "launch": 1.0, "launched": 1.0,
    # ----- bearish -----
    "bearish": -2.5, "bear": -2.0, "crash": -2.8, "crashes": -2.8,
    "crashed": -2.8, "plunge": -2.5, "plunges": -2.5, "plunged": -2.5,
    "tumble": -2.3, "tumbles": -2.3, "tumbled": -2.3, "dump": -2.5,
    "rug": -3.0, "rugpull": -3.0, "exploit": -2.8, "exploited": -2.8,
    "hack": -2.7, "hacked": -2.7, "loss": -1.8, "losses": -1.8, "drop": -1.3,
    "drops": -1.3, "dropped": -1.3, "down": -0.8, "fall": -1.5, "falls": -1.5,
    "fell": -1.5, "weak": -1.4, "weakness": -1.4, "underperform": -2.0,
    "downgrade": -1.7, "downgraded": -1.7, "sell": -1.3, "rejected": -1.5,
    "denied": -1.5, "fraud": -2.7, "lawsuit": -2.0, "investigation": -1.8,
    "ban": -2.0, "banned": -2.0, "halt": -1.5, "halted": -1.5, "delist": -2.2,
    "delisted": -2.2, "concern": -1.0, "concerns": -1.0, "warning": -1.5,
}


class Scorer(Protocol):
    """Structural protocol for sentiment scorers."""

    #: Stable registry key (used in `metrics.json` / Parquet `scorer` col).
    name: str

    def score(self, text: str) -> float:
        """Return a compound polarity score in [-1, 1] for `text`."""
        ...


@dataclass(frozen=True, slots=True)
class VaderScorer:
    """VADER-style compound lexicon scorer (in-tree baseline).

    Compound score::

        compound = total / sqrt(total**2 + alpha)

    where `total` is the negation-adjusted sum of per-token lexicon
    hits and `alpha=15` matches the VADER paper's normalisation.

    Negation handling: a negator (``not`` / ``no`` / ``n't`` / ...)
    within ``_NEGATION_WINDOW`` tokens BEFORE a polarity word inverts
    that word's sign. Multi-word negation chains (e.g. "not not") are
    treated as a single flip (we only check presence, not parity).
    """

    name: str = "vader_lite"
    alpha: float = 15.0

    def score(self, text: str) -> float:
        if not text:
            return 0.0
        tokens = [t.lower() for t in _TOKEN_RE.findall(text)]
        if not tokens:
            return 0.0
        total = 0.0
        for idx, tok in enumerate(tokens):
            base = _LEXICON.get(tok)
            if base is None:
                continue
            window = tokens[max(0, idx - _NEGATION_WINDOW) : idx]
            if any(w in _NEGATORS for w in window):
                base = -base
            total += base
        if total == 0.0:
            return 0.0
        return total / math.sqrt(total * total + self.alpha)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


_SCORERS: dict[str, Callable[[], Scorer]] = {}


def register_scorer(name: str, factory: Callable[[], Scorer]) -> None:
    """Register a `Scorer` factory under `name`.

    Idempotent under same factory (re-registering the same callable is a
    no-op so module re-imports in tests do not raise).
    """

    if not name or not isinstance(name, str):
        raise ValueError(f"scorer name must be a non-empty string, got {name!r}")
    existing = _SCORERS.get(name)
    if existing is not None and existing is not factory:
        raise ValueError(
            f"scorer name {name!r} already registered to a different factory"
        )
    _SCORERS[name] = factory


def get_scorer(name: str) -> Scorer:
    """Instantiate a registered scorer by name."""
    factory = _SCORERS.get(name)
    if factory is None:
        raise ValueError(
            f"unknown scorer {name!r}; available: {available_scorers()}"
        )
    return factory()


def available_scorers() -> list[str]:
    return sorted(_SCORERS)


# Register the in-tree baseline. ``"vader"`` is kept as an alias so the
# CLI `--scorer vader` works without operators having to remember the
# ``_lite`` suffix.
register_scorer("vader_lite", VaderScorer)
register_scorer("vader", VaderScorer)
