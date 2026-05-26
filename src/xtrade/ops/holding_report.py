"""Phase 6 Task T11 — daily holding report.

Produces a deterministic daily snapshot of one instrument's position
state: average entry, current mark + implied mcap, position size,
realized + unrealized P&L, drawdown HWM, TP ladder progress, soft-kill
headroom, cumulative funding paid.

This module is **deliberately pure**: the headline aggregator
:func:`compute_holding_report` takes plain inputs (a list of fills, a
mark price Decimal, a list of ladder rung snapshots, the soft-kill
trigger, etc.) and returns a :class:`HoldingReport` dataclass. The CLI
in :mod:`xtrade.cli` does the I/O (read fills jsonl, read drawdown
state json, read instrument_meta yaml, write the output json, dispatch
an info alert).

Schema (brief §5 T11):

```json
{
  "date": "2026-MM-DD",
  "instrument": "SPCXUSDT-PERP.BINANCE",
  "avg_entry_usd": "...",
  "current_mark_usd": "...",
  "current_mcap_usd": "...",
  "pos_size": "...",
  "unrealized_pnl_usd": "...",
  "realized_pnl_usd": "...",
  "hwm_drawdown_pct": "...",
  "tp_ladder_state": [
    {"target_mcap_usd": "2000000000000",
     "target_mark_usd": "168.49",
     "filled_qty": "...",
     "open_qty": "..."}, ...
  ],
  "soft_kill_distance": {
    "mcap_now_usd": "...",
    "mcap_trigger_usd": "3500000000000",
    "headroom_pct": "..."
  },
  "funding_paid_cumulative_usd": "...",
  "generated_at": "...Z"
}
```

P&L accounting (weighted-average / running-avg method):

  Iterate fills in chronological order. Track ``(avg_entry, open_qty)``
  state for the open leg. Entry fills (``reduce_only=False``) update the
  VWAP and grow ``open_qty``. Reducing fills (``reduce_only=True``)
  realize P&L against ``avg_entry`` and shrink ``open_qty``:

  - direction=short:  realized += (avg_entry - fill_price) * fill_qty
  - direction=long:   realized += (fill_price - avg_entry) * fill_qty

  Unrealized P&L on the still-open leg at the end of the day uses the
  same mirror formula vs the snapshot ``current_mark``.

Direction is inferred from the **first entry fill**: if the first
non-reduce-only fill is a SELL the position is short; BUY → long. This
matches the live-strategy invariant where a single instance is one
direction for its lifetime (T5 ctor enforces ``intent.side`` matches
``cfg.direction``).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from xtrade.instruments.meta import InstrumentMeta
from xtrade.obs.log_event import emit_event


log = logging.getLogger("xtrade.ops.holding_report")

UTC = dt.timezone.utc

Direction = Literal["short", "long"]
SoftKillBoundary = Literal["above", "below"]
Side = Literal["BUY", "SELL"]


class HoldingReportError(ValueError):
    """Raised when compute_holding_report inputs are inconsistent."""


# ---------------------------------------------------------------------------
# Input dataclasses
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FillRow:
    """One executed fill, chronologically ordered.

    ``reduce_only=False`` means an *entry* fill (grows the position
    leg). ``reduce_only=True`` means a *reducing* fill (e.g. TP rung).
    """

    ts: dt.datetime
    side: Side
    qty: Decimal
    price: Decimal
    reduce_only: bool

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise HoldingReportError(
                f"FillRow.ts must be timezone-aware (UTC), got {self.ts!r}"
            )
        if self.side not in ("BUY", "SELL"):
            raise HoldingReportError(
                f"FillRow.side must be 'BUY' or 'SELL', got {self.side!r}"
            )
        if not isinstance(self.qty, Decimal):
            raise HoldingReportError(
                f"FillRow.qty must be Decimal, got {type(self.qty).__name__}"
            )
        if not isinstance(self.price, Decimal):
            raise HoldingReportError(
                f"FillRow.price must be Decimal, got {type(self.price).__name__}"
            )
        if self.qty <= 0:
            raise HoldingReportError(f"FillRow.qty must be > 0, got {self.qty}")
        if self.price <= 0:
            raise HoldingReportError(f"FillRow.price must be > 0, got {self.price}")


@dataclasses.dataclass(frozen=True)
class TpLadderRung:
    """One TP rung snapshot: target mcap + how much has filled."""

    target_mcap_usd: Decimal
    filled_qty: Decimal
    open_qty: Decimal

    def __post_init__(self) -> None:
        for fld in ("target_mcap_usd", "filled_qty", "open_qty"):
            v = getattr(self, fld)
            if not isinstance(v, Decimal):
                raise HoldingReportError(
                    f"TpLadderRung.{fld} must be Decimal, got {type(v).__name__}"
                )
        if self.target_mcap_usd <= 0:
            raise HoldingReportError(
                f"TpLadderRung.target_mcap_usd must be > 0, got {self.target_mcap_usd}"
            )
        if self.filled_qty < 0:
            raise HoldingReportError(
                f"TpLadderRung.filled_qty must be >= 0, got {self.filled_qty}"
            )
        if self.open_qty < 0:
            raise HoldingReportError(
                f"TpLadderRung.open_qty must be >= 0, got {self.open_qty}"
            )


@dataclasses.dataclass(frozen=True)
class TpLadderRungOut:
    """One TP rung as rendered in the report (with computed target_mark)."""

    target_mcap_usd: Decimal
    target_mark_usd: Decimal
    filled_qty: Decimal
    open_qty: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "target_mcap_usd": _fmt(self.target_mcap_usd),
            "target_mark_usd": _fmt(self.target_mark_usd),
            "filled_qty": _fmt(self.filled_qty),
            "open_qty": _fmt(self.open_qty),
        }


@dataclasses.dataclass(frozen=True)
class SoftKillDistanceOut:
    mcap_now_usd: Decimal
    mcap_trigger_usd: Decimal
    headroom_pct: Decimal

    def to_dict(self) -> dict[str, str]:
        return {
            "mcap_now_usd": _fmt(self.mcap_now_usd),
            "mcap_trigger_usd": _fmt(self.mcap_trigger_usd),
            "headroom_pct": _fmt(self.headroom_pct),
        }


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class HoldingReport:
    """Rendered daily snapshot — JSON-serialisable via :meth:`to_dict`."""

    date: dt.date
    instrument: str
    avg_entry_usd: Decimal
    current_mark_usd: Decimal
    current_mcap_usd: Decimal
    pos_size: Decimal
    unrealized_pnl_usd: Decimal
    realized_pnl_usd: Decimal
    hwm_drawdown_pct: Decimal
    tp_ladder_state: tuple[TpLadderRungOut, ...]
    soft_kill_distance: SoftKillDistanceOut
    funding_paid_cumulative_usd: Decimal
    generated_at: dt.datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "instrument": self.instrument,
            "avg_entry_usd": _fmt(self.avg_entry_usd),
            "current_mark_usd": _fmt(self.current_mark_usd),
            "current_mcap_usd": _fmt(self.current_mcap_usd),
            "pos_size": _fmt(self.pos_size),
            "unrealized_pnl_usd": _fmt(self.unrealized_pnl_usd),
            "realized_pnl_usd": _fmt(self.realized_pnl_usd),
            "hwm_drawdown_pct": _fmt(self.hwm_drawdown_pct),
            "tp_ladder_state": [r.to_dict() for r in self.tp_ladder_state],
            "soft_kill_distance": self.soft_kill_distance.to_dict(),
            "funding_paid_cumulative_usd": _fmt(self.funding_paid_cumulative_usd),
            "generated_at": self.generated_at.astimezone(UTC).isoformat(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt(d: Decimal) -> str:
    """Render a Decimal as a canonical string (no scientific notation)."""
    if not isinstance(d, Decimal):
        raise HoldingReportError(f"expected Decimal, got {type(d).__name__}")
    # ``format(d, 'f')`` avoids scientific notation while preserving sign.
    return format(d, "f")


def _infer_direction(fills: Sequence[FillRow]) -> Direction:
    """The position direction is set by the first *entry* fill.

    Raises if no entry fill exists (e.g. the operator passed only
    reducing fills — would mean position was opened on a different day
    and the daily snapshot must be told the direction explicitly).
    """
    for f in fills:
        if not f.reduce_only:
            return "short" if f.side == "SELL" else "long"
    raise HoldingReportError(
        "cannot infer direction: no entry (reduce_only=False) fill in the input; "
        "pass direction explicitly to compute_holding_report(...)"
    )


def _aggregate_position(
    fills: Sequence[FillRow],
    *,
    direction: Direction,
) -> tuple[Decimal, Decimal, Decimal]:
    """Run the weighted-average accounting pass.

    Returns ``(avg_entry, open_qty, realized_pnl)``.

    ``open_qty`` is always non-negative — the caller emits a signed
    ``pos_size`` (negative for short) at report time.
    """
    avg_entry = Decimal("0")
    open_qty = Decimal("0")
    realized = Decimal("0")

    entry_side: Side = "SELL" if direction == "short" else "BUY"
    reducing_side: Side = "BUY" if direction == "short" else "SELL"

    for f in fills:
        if not f.reduce_only:
            # Entry fill: must match the position's entry side.
            if f.side != entry_side:
                raise HoldingReportError(
                    f"entry fill side {f.side!r} inconsistent with "
                    f"direction={direction!r} (expected {entry_side!r}); "
                    f"ts={f.ts.isoformat()}"
                )
            new_qty = open_qty + f.qty
            avg_entry = (
                (avg_entry * open_qty + f.price * f.qty) / new_qty
                if new_qty > 0
                else Decimal("0")
            )
            open_qty = new_qty
        else:
            # Reducing fill: must be the opposite side.
            if f.side != reducing_side:
                raise HoldingReportError(
                    f"reducing fill side {f.side!r} inconsistent with "
                    f"direction={direction!r} (expected {reducing_side!r}); "
                    f"ts={f.ts.isoformat()}"
                )
            if f.qty > open_qty:
                raise HoldingReportError(
                    f"reducing fill qty {f.qty} > open_qty {open_qty} "
                    f"at ts={f.ts.isoformat()}; over-reduce not allowed"
                )
            if direction == "short":
                realized += (avg_entry - f.price) * f.qty
            else:
                realized += (f.price - avg_entry) * f.qty
            open_qty -= f.qty
            # avg_entry preserved (running-avg method).

    return avg_entry, open_qty, realized


def _headroom_pct(
    *, mcap_now: Decimal, mcap_trigger: Decimal, boundary: SoftKillBoundary,
) -> Decimal:
    """Distance from current mcap to soft-kill trigger as a fraction.

    Positive means safe (away from trigger); zero means at trigger;
    negative means already breached.

    - boundary=above (short bias): headroom = (trigger - now) / trigger
    - boundary=below (long bias):  headroom = (now - trigger) / trigger
    """
    if mcap_trigger <= 0:
        raise HoldingReportError(
            f"mcap_trigger must be > 0, got {mcap_trigger}"
        )
    if boundary == "above":
        return (mcap_trigger - mcap_now) / mcap_trigger
    if boundary == "below":
        return (mcap_now - mcap_trigger) / mcap_trigger
    raise HoldingReportError(
        f"soft_kill_boundary must be 'above' or 'below', got {boundary!r}"
    )


# ---------------------------------------------------------------------------
# Pure aggregator
# ---------------------------------------------------------------------------


def compute_holding_report(
    *,
    date: dt.date,
    instrument: str,
    fills: Sequence[FillRow],
    current_mark_usd: Decimal,
    meta: InstrumentMeta,
    hwm_drawdown_pct: Decimal,
    tp_ladder: Sequence[TpLadderRung],
    soft_kill_trigger_mcap_usd: Decimal,
    soft_kill_boundary: SoftKillBoundary,
    funding_paid_cumulative_usd: Decimal,
    direction: Direction | None = None,
    generated_at: dt.datetime | None = None,
) -> HoldingReport:
    """Aggregate fills + state inputs into a :class:`HoldingReport`.

    `direction` may be ``None`` — it will be inferred from the first
    entry fill (see :func:`_infer_direction`).

    Raises :class:`HoldingReportError` on any inconsistency (mixed
    direction fills, over-reduce, bad bounds, etc.).
    """
    if not isinstance(current_mark_usd, Decimal):
        raise HoldingReportError(
            f"current_mark_usd must be Decimal, got {type(current_mark_usd).__name__}"
        )
    if current_mark_usd <= 0:
        raise HoldingReportError(f"current_mark_usd must be > 0, got {current_mark_usd}")
    if not isinstance(hwm_drawdown_pct, Decimal):
        raise HoldingReportError("hwm_drawdown_pct must be Decimal")
    if not isinstance(funding_paid_cumulative_usd, Decimal):
        raise HoldingReportError("funding_paid_cumulative_usd must be Decimal")
    if not isinstance(soft_kill_trigger_mcap_usd, Decimal):
        raise HoldingReportError("soft_kill_trigger_mcap_usd must be Decimal")
    if not isinstance(meta, InstrumentMeta):
        raise HoldingReportError(
            f"meta must be InstrumentMeta, got {type(meta).__name__}"
        )
    if not instrument:
        raise HoldingReportError("instrument must be a non-empty str")

    # ----- direction -------------------------------------------------------
    if direction is None:
        if fills:
            direction = _infer_direction(fills)
        else:
            raise HoldingReportError(
                "cannot infer direction with empty fills; pass direction= explicitly"
            )
    if direction not in ("short", "long"):
        raise HoldingReportError(
            f"direction must be 'short' or 'long', got {direction!r}"
        )

    # ----- position aggregation -------------------------------------------
    avg_entry, open_qty, realized = _aggregate_position(fills, direction=direction)

    # ----- mcap + signed pos_size -----------------------------------------
    current_mcap = current_mark_usd * meta.shares_outstanding
    pos_size = -open_qty if direction == "short" else open_qty

    # ----- unrealized -----------------------------------------------------
    if open_qty > 0:
        if direction == "short":
            unrealized = (avg_entry - current_mark_usd) * open_qty
        else:
            unrealized = (current_mark_usd - avg_entry) * open_qty
    else:
        unrealized = Decimal("0")

    # ----- TP ladder snapshot (compute target_mark from target_mcap) ------
    shares = meta.shares_outstanding
    rendered_ladder: list[TpLadderRungOut] = []
    for rung in tp_ladder:
        target_mark = rung.target_mcap_usd / shares
        rendered_ladder.append(
            TpLadderRungOut(
                target_mcap_usd=rung.target_mcap_usd,
                target_mark_usd=target_mark,
                filled_qty=rung.filled_qty,
                open_qty=rung.open_qty,
            )
        )

    # ----- soft-kill distance ---------------------------------------------
    headroom = _headroom_pct(
        mcap_now=current_mcap,
        mcap_trigger=soft_kill_trigger_mcap_usd,
        boundary=soft_kill_boundary,
    )
    sk_distance = SoftKillDistanceOut(
        mcap_now_usd=current_mcap,
        mcap_trigger_usd=soft_kill_trigger_mcap_usd,
        headroom_pct=headroom,
    )

    return HoldingReport(
        date=date,
        instrument=instrument,
        avg_entry_usd=avg_entry,
        current_mark_usd=current_mark_usd,
        current_mcap_usd=current_mcap,
        pos_size=pos_size,
        unrealized_pnl_usd=unrealized,
        realized_pnl_usd=realized,
        hwm_drawdown_pct=hwm_drawdown_pct,
        tp_ladder_state=tuple(rendered_ladder),
        soft_kill_distance=sk_distance,
        funding_paid_cumulative_usd=funding_paid_cumulative_usd,
        generated_at=generated_at or dt.datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# I/O helpers — used by the CLI; tested via tests/test_cli_holding_report
# ---------------------------------------------------------------------------


def write_report_json(report: HoldingReport, output_path: Path) -> Path:
    """Atomically write ``report.to_dict()`` as pretty JSON to ``output_path``.

    The parent directory is created with 0o755; the file lands as 0o640
    (operator-readable, world-not). Reuses the standard mkstemp/fsync/
    replace template used by Sentinel and DrawdownWatcher.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{output_path.stem}.",
        suffix=".json.tmp",
        dir=str(output_path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report.to_dict(), fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, output_path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    try:
        os.chmod(output_path, 0o640)
    except OSError:  # pragma: no cover - non-posix or read-only fs
        pass
    return output_path


def load_fills_jsonl(path: Path) -> list[FillRow]:
    """Load a chronologically-ordered fills journal.

    Each line is a JSON object with keys ``ts`` (ISO8601 UTC), ``side``
    (``"BUY"``/``"SELL"``), ``qty`` (string/numeric), ``price``
    (string/numeric), ``reduce_only`` (bool). Other keys are ignored.

    No file → empty list. Bad lines → :class:`HoldingReportError`
    pinpointing the line number.
    """
    path = Path(path)
    if not path.exists():
        return []
    rows: list[FillRow] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, 1):
            line = raw.strip()
            if not line:
                continue
            try:
                body = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HoldingReportError(
                    f"{path}:{lineno}: not valid JSON: {exc}"
                ) from exc
            try:
                ts_raw = body["ts"]
                side = body["side"]
                qty = Decimal(str(body["qty"]))
                price = Decimal(str(body["price"]))
                reduce_only = bool(body["reduce_only"])
            except (KeyError, TypeError) as exc:
                raise HoldingReportError(
                    f"{path}:{lineno}: missing or invalid required field: {exc}"
                ) from exc
            ts = dt.datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            rows.append(
                FillRow(
                    ts=ts,
                    side=side,
                    qty=qty,
                    price=price,
                    reduce_only=reduce_only,
                )
            )
    rows.sort(key=lambda r: r.ts)
    return rows


def load_tp_ladder_json(path: Path) -> list[TpLadderRung]:
    """Load a TP ladder snapshot.

    Schema (one JSON file holding a list):
    ``[{"target_mcap_usd": "...", "filled_qty": "...", "open_qty": "..."}, ...]``

    No file → empty list.
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HoldingReportError(
            f"{path}: not valid JSON: {exc}"
        ) from exc
    if not isinstance(body, list):
        raise HoldingReportError(
            f"{path}: expected a JSON list, got {type(body).__name__}"
        )
    out: list[TpLadderRung] = []
    for idx, row in enumerate(body):
        if not isinstance(row, dict):
            raise HoldingReportError(
                f"{path}[{idx}]: expected a JSON object, got {type(row).__name__}"
            )
        try:
            out.append(
                TpLadderRung(
                    target_mcap_usd=Decimal(str(row["target_mcap_usd"])),
                    filled_qty=Decimal(str(row.get("filled_qty", "0"))),
                    open_qty=Decimal(str(row.get("open_qty", "0"))),
                )
            )
        except (KeyError, TypeError) as exc:
            raise HoldingReportError(
                f"{path}[{idx}]: missing or invalid required field: {exc}"
            ) from exc
    return out


def load_drawdown_state_pct(path: Path) -> Decimal:
    """Read ``drawdown_pct`` from the DrawdownWatcher state file.

    Missing file → ``Decimal("0")`` (no drawdown observed yet).
    Schema reference: :class:`xtrade.live.drawdown.DrawdownState`.
    """
    path = Path(path)
    if not path.exists():
        return Decimal("0")
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HoldingReportError(
            f"{path}: not valid JSON: {exc}"
        ) from exc
    if not isinstance(body, dict):
        raise HoldingReportError(
            f"{path}: expected JSON object, got {type(body).__name__}"
        )
    raw = body.get("drawdown_pct", "0")
    try:
        return Decimal(str(raw))
    except Exception as exc:
        raise HoldingReportError(
            f"{path}: drawdown_pct not a Decimal-convertible value: {raw!r}"
        ) from exc


def summary_alert_fields(report: HoldingReport) -> dict[str, Any]:
    """Build the brief-§5-T11 alert summary payload.

    Keys: ``avg_entry``, ``current_mark``, ``unrealized_pnl``,
    ``soft_kill_headroom_pct``. All values are str-formatted Decimals.
    """
    return {
        "avg_entry": _fmt(report.avg_entry_usd),
        "current_mark": _fmt(report.current_mark_usd),
        "unrealized_pnl": _fmt(report.unrealized_pnl_usd),
        "soft_kill_headroom_pct": _fmt(report.soft_kill_distance.headroom_pct),
    }


def emit_report_event(report: HoldingReport) -> None:
    """Emit a structured `ops.holding_report.daily` event for journald."""
    emit_event(
        log,
        "ops.holding_report.daily",
        date=report.date.isoformat(),
        instrument=report.instrument,
        avg_entry_usd=_fmt(report.avg_entry_usd),
        current_mark_usd=_fmt(report.current_mark_usd),
        pos_size=_fmt(report.pos_size),
        unrealized_pnl_usd=_fmt(report.unrealized_pnl_usd),
        realized_pnl_usd=_fmt(report.realized_pnl_usd),
        soft_kill_headroom_pct=_fmt(report.soft_kill_distance.headroom_pct),
    )


__all__ = [
    "Direction",
    "FillRow",
    "HoldingReport",
    "HoldingReportError",
    "SoftKillBoundary",
    "SoftKillDistanceOut",
    "TpLadderRung",
    "TpLadderRungOut",
    "compute_holding_report",
    "emit_report_event",
    "load_drawdown_state_pct",
    "load_fills_jsonl",
    "load_tp_ladder_json",
    "summary_alert_fields",
    "write_report_json",
]
