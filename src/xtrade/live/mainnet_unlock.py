"""Phase 5 Task A5 — mainnet unlock (third lock).

Defense in depth around Phase 3's double-lock:

  Lock 1 — `xtrade.node.factory._assert_testnet_only(venues_cfg)` raises
           `MainnetRefusedError` whenever any venue routes to mainnet.
           Currently hard-rejects without exception.
  Lock 2 — `xtrade.config._validate_enum` rejects yaml `environment`
           values outside the `{TESTNET, DEMO, LIVE}` (binance) /
           `{TESTNET, LIVE}` (hyperliquid) enumerations.
  Lock 3 — *this module* — when venue config DOES route mainnet, the
           caller must additionally satisfy an unlock ritual:
             1. env[`XTRADE_MAINNET_UNLOCK_TOKEN`] is set and non-empty.
             2. `/etc/xtrade/mainnet_unlock` exists, mode == 0o400,
                uid == 0 (root-owned).
             3. The file's first line (stripped) matches the env token.

Phase 5 wires Lock 3 alongside every Lock 1 call site. In current Phase 5
the double-lock still fires first and rejects mainnet, so Lock 3 is
unreachable in production code paths. It becomes load-bearing in Phase 6
when mainnet tap tests start: the operator's deliberate unlock ritual
(generate token → write the 0400 file → set the env var) is the formal
"yes I really mean mainnet" signal.

The error class deliberately does **not** include the token value in any
message; the only thing leaked on failure is `which condition failed`.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Callable, Mapping

from xtrade.config import VenuesConfig


MAINNET_UNLOCK_TOKEN_ENV = "XTRADE_MAINNET_UNLOCK_TOKEN"
DEFAULT_UNLOCK_PATH = Path("/etc/xtrade/mainnet_unlock")

# Mirror of `xtrade.node.factory._{BINANCE,HL}_TESTNET_LIKE` — replicated
# here to avoid a heavy import (factory pulls Nautilus on type-check) and
# so that updating the testnet set requires touching both Lock 1 and
# Lock 3 (forcing review across the pair).
_BINANCE_TESTNET_LIKE = frozenset({"TESTNET", "DEMO"})
_HL_TESTNET_LIKE = frozenset({"TESTNET"})


class MainnetUnlockError(RuntimeError):
    """Raised when mainnet venues are configured but the Phase 5 unlock
    ritual has not been satisfied. Message intentionally never includes
    the unlock token value.
    """


def is_mainnet_venue(venues: VenuesConfig) -> bool:
    """True if any venue routes to a non-testnet environment.

    Phase 6 promoted this from `_is_mainnet` to a public helper so that
    other supervisor-path checks (e.g. `assert_mainnet_risk_ceiling` on
    risk config) can gate on the same mainnet detection without
    re-implementing it.
    """
    b = venues.binance
    if b is not None:
        if b.spot is not None and b.spot.environment not in _BINANCE_TESTNET_LIKE:
            return True
        if b.futures is not None and b.futures.environment not in _BINANCE_TESTNET_LIKE:
            return True
    h = venues.hyperliquid
    if h is not None and h.environment not in _HL_TESTNET_LIKE:
        return True
    return False


# Back-compat alias for any pre-Phase-6 internal caller; new code should
# use the public name.
_is_mainnet = is_mainnet_venue


def assert_mainnet_unlock(
    venues: VenuesConfig,
    *,
    env: Mapping[str, str] | None = None,
    unlock_path: Path | str = DEFAULT_UNLOCK_PATH,
    stat_fn: Callable[[Path], os.stat_result] | None = None,
) -> None:
    """Raise `MainnetUnlockError` if mainnet routing is configured but
    the unlock ritual is incomplete.

    On testnet-only configs this is a no-op (returns without checking
    the env or the unlock file at all).

    Parameters
    ----------
    venues
        Loaded `VenuesConfig`; same object the factory inspects.
    env
        Override for `os.environ` (tests inject a controlled mapping;
        production passes None ⇒ `os.environ`).
    unlock_path
        Override for `/etc/xtrade/mainnet_unlock`. Tests point to
        tmp_path / "unlock".
    stat_fn
        Override for `path.stat()`. Tests inject a synthetic stat
        result so they can assert `st_uid == 0` and `st_mode == 0o400`
        checks without needing actual root ownership.
    """
    if not is_mainnet_venue(venues):
        return

    env_map = env if env is not None else os.environ
    token = (env_map.get(MAINNET_UNLOCK_TOKEN_ENV) or "").strip()
    if not token:
        raise MainnetUnlockError(
            f"mainnet routing requested but {MAINNET_UNLOCK_TOKEN_ENV} env var "
            f"is unset or empty; Phase 5 third lock denies startup."
        )

    path = Path(unlock_path)
    stat_call = stat_fn if stat_fn is not None else (lambda p: p.stat())
    try:
        st = stat_call(path)
    except FileNotFoundError as exc:
        raise MainnetUnlockError(
            f"mainnet routing requested but unlock file {path!s} is missing."
        ) from exc
    except OSError as exc:
        raise MainnetUnlockError(
            f"unlock file {path!s} unreadable: {type(exc).__name__}"
        ) from exc

    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o400:
        raise MainnetUnlockError(
            f"unlock file {path!s} has insecure mode {oct(mode)}; "
            f"require 0o400 (read-only, owner only)."
        )
    if st.st_uid != 0:
        raise MainnetUnlockError(
            f"unlock file {path!s} must be owned by root (uid 0); "
            f"got uid {st.st_uid}."
        )

    try:
        text = path.read_text()
    except OSError as exc:
        raise MainnetUnlockError(
            f"unlock file {path!s} unreadable on read: {type(exc).__name__}"
        ) from exc

    lines = text.splitlines()
    if not lines:
        raise MainnetUnlockError(f"unlock file {path!s} is empty.")
    first = lines[0].strip()
    if first != token:
        raise MainnetUnlockError(
            f"unlock token in {path!s} does not match {MAINNET_UNLOCK_TOKEN_ENV}; "
            f"refusing to unlock mainnet."
        )
    # All three conditions satisfied → caller proceeds.


__all__ = [
    "DEFAULT_UNLOCK_PATH",
    "MAINNET_UNLOCK_TOKEN_ENV",
    "MainnetUnlockError",
    "assert_mainnet_unlock",
    "is_mainnet_venue",
]
