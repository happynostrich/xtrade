"""OrderIntent + Fill dataclasses (Phase 3 Task 1 / T2).

The strategy layer never builds Nautilus `Order` objects directly —
it emits `OrderIntent` records, which then traverse the risk → approval
→ execution chain in `xtrade.strategy.runner`. Keeping money quantities
as `Decimal` end-to-end means we round-trip through JSON without losing
precision, and we avoid float-vs-Decimal mixed arithmetic (see Phase 3
brief §6).

Serialisation contract
----------------------
`OrderIntent.to_dict()` / `from_dict()` produce JSON-safe dicts where
all `Decimal` fields are encoded as their `str()` representation. The
`ApprovalQueue` (Task 3) writes these to jsonl; we want byte-identical
round-trips so the dedup hash and audit trail stay stable.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from decimal import Decimal
from typing import Any, Literal


Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]
TimeInForce = Literal["GTC", "IOC", "FOK", "DAY"]

_VALID_SIDES: frozenset[str] = frozenset({"BUY", "SELL"})
_VALID_ORDER_TYPES: frozenset[str] = frozenset({"MARKET", "LIMIT"})
_VALID_TIF: frozenset[str] = frozenset({"GTC", "IOC", "FOK", "DAY"})


class OrderIntentError(ValueError):
    """Raised when an `OrderIntent` fails its construction-time checks."""


@dataclasses.dataclass(frozen=True, slots=True)
class OrderIntent:
    """A strategy's request to submit one order.

    Field meanings
    --------------
    venue            : venue key, e.g. ``binance`` / ``hyperliquid``.
    symbol           : exchange ticker / Nautilus instrument id stem,
                       e.g. ``BTCUSDT-PERP.BINANCE``.
    side             : ``BUY`` / ``SELL``.
    order_type       : ``MARKET`` / ``LIMIT``.
    quantity         : positive ``Decimal`` (instrument units).
    limit_price      : required when ``order_type == "LIMIT"``; must be None
                       for ``MARKET``.
    reduce_only      : if True, intent must shrink (not flip / extend) net
                       exposure on ``symbol``. RiskGate enforces.
    time_in_force    : ``GTC`` / ``IOC`` / ``FOK`` / ``DAY``.
    source_signal_id : dedup key referencing the originating signal
                       (``f"{generated_at}|{symbol}|{source}"``); empty
                       string allowed when the intent was hand-rolled.
    created_at       : UTC tz-aware datetime when the intent was minted.
    metadata         : free-form audit trail; same credential-scan rules
                       as Phase 2 `Signal.metadata` should apply on the
                       writer side (ApprovalQueue), not here.
    """

    venue: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    time_in_force: TimeInForce
    source_signal_id: str
    created_at: dt.datetime
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.venue or not isinstance(self.venue, str):
            raise OrderIntentError(
                f"venue must be a non-empty string, got {self.venue!r}"
            )
        if not self.symbol or not isinstance(self.symbol, str):
            raise OrderIntentError(
                f"symbol must be a non-empty string, got {self.symbol!r}"
            )
        if self.side not in _VALID_SIDES:
            raise OrderIntentError(
                f"side must be one of {sorted(_VALID_SIDES)}, got {self.side!r}"
            )
        if self.order_type not in _VALID_ORDER_TYPES:
            raise OrderIntentError(
                f"order_type must be one of {sorted(_VALID_ORDER_TYPES)}, "
                f"got {self.order_type!r}"
            )
        if not isinstance(self.quantity, Decimal):
            raise OrderIntentError(
                f"quantity must be Decimal, got {type(self.quantity).__name__}"
            )
        if self.quantity <= 0:
            raise OrderIntentError(
                f"quantity must be > 0, got {self.quantity}"
            )
        if self.order_type == "LIMIT":
            if not isinstance(self.limit_price, Decimal):
                raise OrderIntentError(
                    "LIMIT orders require a Decimal limit_price"
                )
            if self.limit_price <= 0:
                raise OrderIntentError(
                    f"limit_price must be > 0, got {self.limit_price}"
                )
        else:  # MARKET
            if self.limit_price is not None:
                raise OrderIntentError(
                    "MARKET orders must not carry a limit_price"
                )
        if self.time_in_force not in _VALID_TIF:
            raise OrderIntentError(
                f"time_in_force must be one of {sorted(_VALID_TIF)}, "
                f"got {self.time_in_force!r}"
            )
        if not isinstance(self.reduce_only, bool):
            raise OrderIntentError(
                f"reduce_only must be bool, got {type(self.reduce_only).__name__}"
            )
        if not isinstance(self.created_at, dt.datetime):
            raise OrderIntentError(
                f"created_at must be datetime, got {type(self.created_at).__name__}"
            )
        if self.created_at.tzinfo is None:
            raise OrderIntentError(
                "created_at must be timezone-aware (UTC)"
            )
        if not isinstance(self.metadata, dict):
            raise OrderIntentError(
                f"metadata must be dict, got {type(self.metadata).__name__}"
            )

    # ----- (de)serialisation helpers --------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "quantity": str(self.quantity),
            "limit_price": (
                str(self.limit_price) if self.limit_price is not None else None
            ),
            "reduce_only": bool(self.reduce_only),
            "time_in_force": self.time_in_force,
            "source_signal_id": self.source_signal_id,
            "created_at": self.created_at.astimezone(dt.timezone.utc).isoformat(),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OrderIntent":
        raw_limit = data.get("limit_price")
        limit_price = Decimal(raw_limit) if raw_limit is not None else None
        created_at = dt.datetime.fromisoformat(data["created_at"])
        if created_at.tzinfo is None:
            raise OrderIntentError("created_at must be tz-aware (UTC)")
        return cls(
            venue=data["venue"],
            symbol=data["symbol"],
            side=data["side"],
            order_type=data["order_type"],
            quantity=Decimal(data["quantity"]),
            limit_price=limit_price,
            reduce_only=bool(data["reduce_only"]),
            time_in_force=data["time_in_force"],
            source_signal_id=data.get("source_signal_id", ""),
            created_at=created_at.astimezone(dt.timezone.utc),
            metadata=dict(data.get("metadata", {})),
        )

    def fingerprint(self) -> str:
        """Stable 16-hex SHA-256 prefix of the intent's JSON.

        Used by `ApprovalQueue` as a deterministic id so re-emitted
        intents (e.g. after a restart) dedup against the existing
        pending row.
        """
        payload = json.dumps(self.to_dict(), sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclasses.dataclass(frozen=True, slots=True)
class Fill:
    """Confirmation that an `OrderIntent` was (partially) executed.

    Emitted by both paper-mode (`xtrade.strategy.runner`) and live-mode
    (Phase 1 `xtrade.live.runner` adapter) so the parity test (T7) can
    compare them field-by-field.
    """

    venue: str
    symbol: str
    side: Side
    quantity: Decimal
    price: Decimal
    ts_event: dt.datetime
    intent_fingerprint: str
    order_id: str = ""
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.venue:
            raise OrderIntentError("Fill.venue must be non-empty")
        if not self.symbol:
            raise OrderIntentError("Fill.symbol must be non-empty")
        if self.side not in _VALID_SIDES:
            raise OrderIntentError(f"Fill.side must be in {_VALID_SIDES}")
        if not isinstance(self.quantity, Decimal) or self.quantity <= 0:
            raise OrderIntentError("Fill.quantity must be Decimal > 0")
        if not isinstance(self.price, Decimal) or self.price <= 0:
            raise OrderIntentError("Fill.price must be Decimal > 0")
        if self.ts_event.tzinfo is None:
            raise OrderIntentError("Fill.ts_event must be tz-aware (UTC)")
        if not isinstance(self.intent_fingerprint, str) or not self.intent_fingerprint:
            raise OrderIntentError(
                "Fill.intent_fingerprint must be a non-empty string"
            )
        if not isinstance(self.metadata, dict):
            raise OrderIntentError("Fill.metadata must be dict")

    def to_dict(self) -> dict[str, Any]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.side,
            "quantity": str(self.quantity),
            "price": str(self.price),
            "ts_event": self.ts_event.astimezone(dt.timezone.utc).isoformat(),
            "intent_fingerprint": self.intent_fingerprint,
            "order_id": self.order_id,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Fill":
        ts = dt.datetime.fromisoformat(data["ts_event"])
        if ts.tzinfo is None:
            raise OrderIntentError("Fill.ts_event must be tz-aware (UTC)")
        return cls(
            venue=data["venue"],
            symbol=data["symbol"],
            side=data["side"],
            quantity=Decimal(data["quantity"]),
            price=Decimal(data["price"]),
            ts_event=ts.astimezone(dt.timezone.utc),
            intent_fingerprint=data["intent_fingerprint"],
            order_id=data.get("order_id", ""),
            metadata=dict(data.get("metadata", {})),
        )
