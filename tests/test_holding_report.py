"""Tests for Phase 6 Task T11 — :mod:`xtrade.ops.holding_report`.

Covers the pure aggregator + the file-format helpers. CLI surface is
tested in :mod:`tests.test_cli_holding_report`.

Key invariants verified:

- Position weighted-average accounting (running-avg method) on
  ``direction=short`` and ``direction=long`` mirror cases.
- Direction inference from the first entry fill.
- Reducing fills against the entry-side raise.
- Over-reduce raises (cannot reduce more than ``open_qty``).
- ``soft_kill_distance.headroom_pct`` flips sign correctly for
  ``boundary=above`` vs ``boundary=below``.
- ``tp_ladder_state[*].target_mark_usd`` = ``target_mcap_usd /
  shares_outstanding``.
- JSON round-trip: every Decimal in the output is a string (no
  scientific notation).
- Atomic write: tmp file vanishes after success; no debris on failure.
- ``load_fills_jsonl`` sorts by ts, skips blank lines, raises with line
  numbers on bad JSON.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from pathlib import Path

import pytest

from xtrade.instruments.meta import InstrumentMeta
from xtrade.ops.holding_report import (
    FillRow,
    HoldingReport,
    HoldingReportError,
    SoftKillDistanceOut,
    TpLadderRung,
    TpLadderRungOut,
    _headroom_pct,
    _infer_direction,
    compute_holding_report,
    load_drawdown_state_pct,
    load_fills_jsonl,
    load_tp_ladder_json,
    summary_alert_fields,
    write_report_json,
)


UTC = dt.timezone.utc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _meta_spcx() -> InstrumentMeta:
    return InstrumentMeta(
        symbol="SPCXUSDT-PERP.BINANCE",
        shares_outstanding=Decimal("11870000000"),
        min_qty=Decimal("0.001"),
        qty_step=Decimal("0.001"),
        tick_size=Decimal("0.01"),
        mark_source="oracle",
    )


def _fill(
    *,
    ts: str,
    side: str,
    qty: str,
    price: str,
    reduce_only: bool,
) -> FillRow:
    return FillRow(
        ts=dt.datetime.fromisoformat(ts.replace("Z", "+00:00")),
        side=side,  # type: ignore[arg-type]
        qty=Decimal(qty),
        price=Decimal(price),
        reduce_only=reduce_only,
    )


# ---------------------------------------------------------------------------
# Input dataclass validation
# ---------------------------------------------------------------------------


class TestFillRowValidation:
    def test_rejects_naive_ts(self) -> None:
        with pytest.raises(HoldingReportError):
            FillRow(
                ts=dt.datetime(2026, 5, 24, 12, 0, 0),
                side="SELL",
                qty=Decimal("1"),
                price=Decimal("220"),
                reduce_only=False,
            )

    def test_rejects_bad_side(self) -> None:
        with pytest.raises(HoldingReportError):
            FillRow(
                ts=dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
                side="LONG",  # type: ignore[arg-type]
                qty=Decimal("1"),
                price=Decimal("220"),
                reduce_only=False,
            )

    def test_rejects_non_decimal_qty(self) -> None:
        with pytest.raises(HoldingReportError):
            FillRow(
                ts=dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
                side="SELL",
                qty=1.0,  # type: ignore[arg-type]
                price=Decimal("220"),
                reduce_only=False,
            )

    def test_rejects_zero_qty(self) -> None:
        with pytest.raises(HoldingReportError):
            FillRow(
                ts=dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
                side="SELL",
                qty=Decimal("0"),
                price=Decimal("220"),
                reduce_only=False,
            )

    def test_rejects_negative_price(self) -> None:
        with pytest.raises(HoldingReportError):
            FillRow(
                ts=dt.datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC),
                side="SELL",
                qty=Decimal("1"),
                price=Decimal("-1"),
                reduce_only=False,
            )


class TestTpLadderRungValidation:
    def test_rejects_non_decimal(self) -> None:
        with pytest.raises(HoldingReportError):
            TpLadderRung(
                target_mcap_usd=2_000_000_000_000,  # type: ignore[arg-type]
                filled_qty=Decimal("0"),
                open_qty=Decimal("1"),
            )

    def test_rejects_zero_target(self) -> None:
        with pytest.raises(HoldingReportError):
            TpLadderRung(
                target_mcap_usd=Decimal("0"),
                filled_qty=Decimal("0"),
                open_qty=Decimal("1"),
            )

    def test_rejects_negative_filled(self) -> None:
        with pytest.raises(HoldingReportError):
            TpLadderRung(
                target_mcap_usd=Decimal("2e12"),
                filled_qty=Decimal("-1"),
                open_qty=Decimal("1"),
            )


# ---------------------------------------------------------------------------
# Direction inference
# ---------------------------------------------------------------------------


class TestDirectionInference:
    def test_first_entry_sell_means_short(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="0.5",
                  price="220", reduce_only=False),
        ]
        assert _infer_direction(fills) == "short"

    def test_first_entry_buy_means_long(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="BUY", qty="0.5",
                  price="220", reduce_only=False),
        ]
        assert _infer_direction(fills) == "long"

    def test_skip_reducing_fills_before_first_entry(self) -> None:
        # Shouldn't happen in practice, but defensive
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="BUY", qty="0.1",
                  price="220", reduce_only=True),
            _fill(ts="2026-05-24T10:05:00Z", side="SELL", qty="0.5",
                  price="220", reduce_only=False),
        ]
        assert _infer_direction(fills) == "short"

    def test_no_entry_fill_raises(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="BUY", qty="0.1",
                  price="220", reduce_only=True),
        ]
        with pytest.raises(HoldingReportError):
            _infer_direction(fills)


# ---------------------------------------------------------------------------
# Position aggregation (the heart of the report)
# ---------------------------------------------------------------------------


class TestPositionAggregationShort:
    """Direction = short. Entry side is SELL; reducing side is BUY."""

    def test_single_entry_no_reduce(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        assert report.avg_entry_usd == Decimal("220")
        assert report.pos_size == Decimal("-1")  # short → signed negative
        # short profits when mark drops below entry: (220 - 210) * 1 = 10
        assert report.unrealized_pnl_usd == Decimal("10")
        assert report.realized_pnl_usd == Decimal("0")

    def test_two_entries_vwap(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
            _fill(ts="2026-05-24T11:00:00Z", side="SELL", qty="1",
                  price="210", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("200"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        # VWAP = (220*1 + 210*1) / 2 = 215
        assert report.avg_entry_usd == Decimal("215")
        assert report.pos_size == Decimal("-2")
        # short: (215 - 200) * 2 = 30
        assert report.unrealized_pnl_usd == Decimal("30")

    def test_entry_then_partial_tp(self) -> None:
        # Open 2 contracts short at $220, close 1 at $200 (TP), mark at $210
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="2",
                  price="220", reduce_only=False),
            _fill(ts="2026-05-24T11:00:00Z", side="BUY", qty="1",
                  price="200", reduce_only=True),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        # avg_entry preserved at 220 (running-avg method)
        assert report.avg_entry_usd == Decimal("220")
        assert report.pos_size == Decimal("-1")  # 1 contract still open
        # realized: (220 - 200) * 1 = 20
        assert report.realized_pnl_usd == Decimal("20")
        # unrealized: (220 - 210) * 1 = 10
        assert report.unrealized_pnl_usd == Decimal("10")

    def test_full_close_zero_unrealized(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
            _fill(ts="2026-05-24T11:00:00Z", side="BUY", qty="1",
                  price="200", reduce_only=True),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        assert report.pos_size == Decimal("0")
        assert report.unrealized_pnl_usd == Decimal("0")
        assert report.realized_pnl_usd == Decimal("20")

    def test_entry_side_buy_rejected(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="BUY", qty="1",
                  price="220", reduce_only=False),
        ]
        with pytest.raises(HoldingReportError):
            compute_holding_report(
                date=dt.date(2026, 5, 24),
                instrument="SPCXUSDT-PERP.BINANCE",
                fills=fills,
                current_mark_usd=Decimal("210"),
                meta=_meta_spcx(),
                hwm_drawdown_pct=Decimal("0"),
                tp_ladder=[],
                soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
                soft_kill_boundary="above",
                funding_paid_cumulative_usd=Decimal("0"),
                direction="short",
            )

    def test_over_reduce_raises(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
            _fill(ts="2026-05-24T11:00:00Z", side="BUY", qty="2",
                  price="200", reduce_only=True),
        ]
        with pytest.raises(HoldingReportError):
            compute_holding_report(
                date=dt.date(2026, 5, 24),
                instrument="SPCXUSDT-PERP.BINANCE",
                fills=fills,
                current_mark_usd=Decimal("210"),
                meta=_meta_spcx(),
                hwm_drawdown_pct=Decimal("0"),
                tp_ladder=[],
                soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
                soft_kill_boundary="above",
                funding_paid_cumulative_usd=Decimal("0"),
            )


class TestPositionAggregationLong:
    """Direction = long. Entry side is BUY; reducing side is SELL."""

    def test_single_entry(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="BUY", qty="1",
                  price="200", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("100000000000"),
            soft_kill_boundary="below",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        assert report.avg_entry_usd == Decimal("200")
        assert report.pos_size == Decimal("1")  # long → positive
        # long: (210 - 200) * 1 = 10
        assert report.unrealized_pnl_usd == Decimal("10")

    def test_entry_then_partial_tp(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="BUY", qty="2",
                  price="200", reduce_only=False),
            _fill(ts="2026-05-24T11:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=True),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("100000000000"),
            soft_kill_boundary="below",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        assert report.avg_entry_usd == Decimal("200")
        assert report.pos_size == Decimal("1")
        # realized: (220 - 200) * 1 = 20
        assert report.realized_pnl_usd == Decimal("20")
        # unrealized: (210 - 200) * 1 = 10
        assert report.unrealized_pnl_usd == Decimal("10")


# ---------------------------------------------------------------------------
# Soft-kill distance + headroom sign flip
# ---------------------------------------------------------------------------


class TestHeadroom:
    def test_above_positive_when_below_trigger(self) -> None:
        # short bias: safe when mcap < trigger
        h = _headroom_pct(
            mcap_now=Decimal("2000000000000"),
            mcap_trigger=Decimal("3500000000000"),
            boundary="above",
        )
        # (3.5T - 2T) / 3.5T = 1.5/3.5
        assert h == Decimal("1500000000000") / Decimal("3500000000000")
        assert h > 0

    def test_above_zero_at_trigger(self) -> None:
        h = _headroom_pct(
            mcap_now=Decimal("3500000000000"),
            mcap_trigger=Decimal("3500000000000"),
            boundary="above",
        )
        assert h == Decimal("0")

    def test_above_negative_after_breach(self) -> None:
        h = _headroom_pct(
            mcap_now=Decimal("4000000000000"),
            mcap_trigger=Decimal("3500000000000"),
            boundary="above",
        )
        assert h < 0

    def test_below_mirror(self) -> None:
        # long bias: safe when mcap > trigger
        h = _headroom_pct(
            mcap_now=Decimal("2000000000000"),
            mcap_trigger=Decimal("1000000000000"),
            boundary="below",
        )
        # (2T - 1T) / 1T = 1.0
        assert h == Decimal("1")

    def test_below_negative_after_breach(self) -> None:
        h = _headroom_pct(
            mcap_now=Decimal("500000000000"),
            mcap_trigger=Decimal("1000000000000"),
            boundary="below",
        )
        assert h < 0

    def test_zero_trigger_rejected(self) -> None:
        with pytest.raises(HoldingReportError):
            _headroom_pct(
                mcap_now=Decimal("1"),
                mcap_trigger=Decimal("0"),
                boundary="above",
            )

    def test_bad_boundary_rejected(self) -> None:
        with pytest.raises(HoldingReportError):
            _headroom_pct(
                mcap_now=Decimal("1"),
                mcap_trigger=Decimal("1"),
                boundary="sideways",  # type: ignore[arg-type]
            )


# ---------------------------------------------------------------------------
# TP ladder rendering (target_mark derived from target_mcap)
# ---------------------------------------------------------------------------


class TestTpLadderRendering:
    def test_target_mark_derived_from_mcap_over_shares(self) -> None:
        ladder = [
            TpLadderRung(
                target_mcap_usd=Decimal("2000000000000"),
                filled_qty=Decimal("0"),
                open_qty=Decimal("0.5"),
            ),
            TpLadderRung(
                target_mcap_usd=Decimal("1500000000000"),
                filled_qty=Decimal("0.5"),
                open_qty=Decimal("0"),
            ),
        ]
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=ladder,
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        # 2T / 11.87B
        expected_mark_2t = Decimal("2000000000000") / Decimal("11870000000")
        assert report.tp_ladder_state[0].target_mark_usd == expected_mark_2t
        # Order preserved
        assert report.tp_ladder_state[1].target_mcap_usd == Decimal(
            "1500000000000"
        )

    def test_empty_ladder_ok(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        assert report.tp_ladder_state == ()


# ---------------------------------------------------------------------------
# Schema / JSON round-trip
# ---------------------------------------------------------------------------


class TestSchema:
    def test_to_dict_keys_match_brief(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0.025"),
            tp_ladder=[
                TpLadderRung(
                    target_mcap_usd=Decimal("2000000000000"),
                    filled_qty=Decimal("0"),
                    open_qty=Decimal("0.5"),
                ),
            ],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0.5"),
            generated_at=dt.datetime(2026, 5, 24, 23, 59, 59, tzinfo=UTC),
        )
        body = report.to_dict()
        expected_keys = {
            "date",
            "instrument",
            "avg_entry_usd",
            "current_mark_usd",
            "current_mcap_usd",
            "pos_size",
            "unrealized_pnl_usd",
            "realized_pnl_usd",
            "hwm_drawdown_pct",
            "tp_ladder_state",
            "soft_kill_distance",
            "funding_paid_cumulative_usd",
            "generated_at",
        }
        assert set(body.keys()) == expected_keys

    def test_decimals_serialise_as_strings(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0.025"),
            tp_ladder=[
                TpLadderRung(
                    target_mcap_usd=Decimal("2000000000000"),
                    filled_qty=Decimal("0"),
                    open_qty=Decimal("0.5"),
                ),
            ],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        body = report.to_dict()
        # Every numeric value in the report must be a string
        for key in (
            "avg_entry_usd",
            "current_mark_usd",
            "current_mcap_usd",
            "pos_size",
            "unrealized_pnl_usd",
            "realized_pnl_usd",
            "hwm_drawdown_pct",
            "funding_paid_cumulative_usd",
        ):
            assert isinstance(body[key], str), key
        sk = body["soft_kill_distance"]
        for k in ("mcap_now_usd", "mcap_trigger_usd", "headroom_pct"):
            assert isinstance(sk[k], str), k
        for rung in body["tp_ladder_state"]:
            for k in ("target_mcap_usd", "target_mark_usd", "filled_qty", "open_qty"):
                assert isinstance(rung[k], str), k
        # Survives a JSON round-trip
        text = json.dumps(body)
        recovered = json.loads(text)
        assert recovered["avg_entry_usd"] == body["avg_entry_usd"]

    def test_no_scientific_notation(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        body = report.to_dict()
        # current_mcap = 210 * 11.87B = 2.4927e12; must NOT come out as
        # scientific notation
        assert "e" not in body["current_mcap_usd"].lower()


# ---------------------------------------------------------------------------
# write_report_json — atomic-write template
# ---------------------------------------------------------------------------


class TestWriteReportJson:
    def _report(self) -> HoldingReport:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        return compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )

    def test_writes_pretty_json_at_path(self, tmp_path: Path) -> None:
        out = tmp_path / "phase6" / "holding_2026-05-24.json"
        written = write_report_json(self._report(), out)
        assert written == out
        assert out.exists()
        body = json.loads(out.read_text(encoding="utf-8"))
        assert body["date"] == "2026-05-24"
        assert body["instrument"] == "SPCXUSDT-PERP.BINANCE"

    def test_no_tmp_debris(self, tmp_path: Path) -> None:
        out = tmp_path / "holding.json"
        write_report_json(self._report(), out)
        siblings = list(tmp_path.iterdir())
        # Only the final file remains
        assert siblings == [out]

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        out = tmp_path / "a" / "b" / "c" / "holding.json"
        write_report_json(self._report(), out)
        assert out.exists()


# ---------------------------------------------------------------------------
# load_fills_jsonl
# ---------------------------------------------------------------------------


class TestLoadFillsJsonl:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_fills_jsonl(tmp_path / "nope.jsonl") == []

    def test_sorts_by_ts(self, tmp_path: Path) -> None:
        p = tmp_path / "fills.jsonl"
        rows = [
            {"ts": "2026-05-24T12:00:00Z", "side": "BUY",
             "qty": "0.5", "price": "200", "reduce_only": True},
            {"ts": "2026-05-24T10:00:00Z", "side": "SELL",
             "qty": "1", "price": "220", "reduce_only": False},
        ]
        p.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n",
            encoding="utf-8",
        )
        loaded = load_fills_jsonl(p)
        assert len(loaded) == 2
        assert loaded[0].ts < loaded[1].ts
        assert loaded[0].side == "SELL"
        assert loaded[1].reduce_only is True

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "fills.jsonl"
        p.write_text(
            '\n'
            '{"ts":"2026-05-24T10:00:00Z","side":"SELL","qty":"1","price":"220","reduce_only":false}\n'
            '\n'
            '   \n',
            encoding="utf-8",
        )
        assert len(load_fills_jsonl(p)) == 1

    def test_bad_json_includes_lineno(self, tmp_path: Path) -> None:
        p = tmp_path / "fills.jsonl"
        p.write_text(
            '{"ts":"2026-05-24T10:00:00Z","side":"SELL","qty":"1","price":"220","reduce_only":false}\n'
            'not-json\n',
            encoding="utf-8",
        )
        with pytest.raises(HoldingReportError) as exc_info:
            load_fills_jsonl(p)
        assert ":2:" in str(exc_info.value)

    def test_missing_required_field(self, tmp_path: Path) -> None:
        p = tmp_path / "fills.jsonl"
        p.write_text(
            '{"ts":"2026-05-24T10:00:00Z","qty":"1","price":"220","reduce_only":false}\n',
            encoding="utf-8",
        )
        with pytest.raises(HoldingReportError):
            load_fills_jsonl(p)

    def test_naive_iso_normalised_to_utc(self, tmp_path: Path) -> None:
        p = tmp_path / "fills.jsonl"
        p.write_text(
            '{"ts":"2026-05-24T10:00:00","side":"SELL","qty":"1","price":"220","reduce_only":false}\n',
            encoding="utf-8",
        )
        loaded = load_fills_jsonl(p)
        assert loaded[0].ts.tzinfo is not None


# ---------------------------------------------------------------------------
# load_tp_ladder_json + load_drawdown_state_pct
# ---------------------------------------------------------------------------


class TestLoadTpLadderJson:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_tp_ladder_json(tmp_path / "nope.json") == []

    def test_parses_list(self, tmp_path: Path) -> None:
        p = tmp_path / "ladder.json"
        p.write_text(
            json.dumps(
                [
                    {"target_mcap_usd": "2000000000000",
                     "filled_qty": "0.25", "open_qty": "0.25"},
                    {"target_mcap_usd": "1500000000000",
                     "filled_qty": "0", "open_qty": "0.5"},
                ]
            ),
            encoding="utf-8",
        )
        rungs = load_tp_ladder_json(p)
        assert len(rungs) == 2
        assert rungs[0].target_mcap_usd == Decimal("2000000000000")
        assert rungs[1].filled_qty == Decimal("0")

    def test_missing_filled_qty_defaults_zero(self, tmp_path: Path) -> None:
        p = tmp_path / "ladder.json"
        p.write_text(
            json.dumps([{"target_mcap_usd": "1e12"}]),
            encoding="utf-8",
        )
        rungs = load_tp_ladder_json(p)
        assert rungs[0].filled_qty == Decimal("0")
        assert rungs[0].open_qty == Decimal("0")

    def test_not_a_list_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "ladder.json"
        p.write_text(json.dumps({"foo": "bar"}), encoding="utf-8")
        with pytest.raises(HoldingReportError):
            load_tp_ladder_json(p)

    def test_bad_json_raises(self, tmp_path: Path) -> None:
        p = tmp_path / "ladder.json"
        p.write_text("not-json", encoding="utf-8")
        with pytest.raises(HoldingReportError):
            load_tp_ladder_json(p)


class TestLoadDrawdownStatePct:
    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        assert load_drawdown_state_pct(tmp_path / "nope.json") == Decimal("0")

    def test_reads_drawdown_pct(self, tmp_path: Path) -> None:
        p = tmp_path / "drawdown.json"
        p.write_text(
            json.dumps(
                {
                    "hwm_usd": "200",
                    "last_equity_usd": "190",
                    "last_update_ts": "2026-05-24T12:00:00+00:00",
                    "halted": False,
                    "drawdown_pct": "0.05",
                    "halt_pct": "0.05",
                }
            ),
            encoding="utf-8",
        )
        assert load_drawdown_state_pct(p) == Decimal("0.05")

    def test_default_zero_when_field_missing(self, tmp_path: Path) -> None:
        p = tmp_path / "drawdown.json"
        p.write_text(json.dumps({"halted": False}), encoding="utf-8")
        assert load_drawdown_state_pct(p) == Decimal("0")


# ---------------------------------------------------------------------------
# summary_alert_fields
# ---------------------------------------------------------------------------


class TestSummaryAlertFields:
    def test_contains_brief_keys(self) -> None:
        fills = [
            _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                  price="220", reduce_only=False),
        ]
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=fills,
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
        )
        fields = summary_alert_fields(report)
        assert set(fields.keys()) == {
            "avg_entry",
            "current_mark",
            "unrealized_pnl",
            "soft_kill_headroom_pct",
        }
        # All Decimal-string values
        for v in fields.values():
            assert isinstance(v, str)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_negative_mark_rejected(self) -> None:
        with pytest.raises(HoldingReportError):
            compute_holding_report(
                date=dt.date(2026, 5, 24),
                instrument="SPCXUSDT-PERP.BINANCE",
                fills=[
                    _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                          price="220", reduce_only=False),
                ],
                current_mark_usd=Decimal("-1"),
                meta=_meta_spcx(),
                hwm_drawdown_pct=Decimal("0"),
                tp_ladder=[],
                soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
                soft_kill_boundary="above",
                funding_paid_cumulative_usd=Decimal("0"),
            )

    def test_empty_fills_with_explicit_direction_ok(self) -> None:
        report = compute_holding_report(
            date=dt.date(2026, 5, 24),
            instrument="SPCXUSDT-PERP.BINANCE",
            fills=[],
            current_mark_usd=Decimal("210"),
            meta=_meta_spcx(),
            hwm_drawdown_pct=Decimal("0"),
            tp_ladder=[],
            soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
            soft_kill_boundary="above",
            funding_paid_cumulative_usd=Decimal("0"),
            direction="short",
        )
        assert report.pos_size == Decimal("0")
        assert report.avg_entry_usd == Decimal("0")

    def test_empty_fills_no_direction_raises(self) -> None:
        with pytest.raises(HoldingReportError):
            compute_holding_report(
                date=dt.date(2026, 5, 24),
                instrument="SPCXUSDT-PERP.BINANCE",
                fills=[],
                current_mark_usd=Decimal("210"),
                meta=_meta_spcx(),
                hwm_drawdown_pct=Decimal("0"),
                tp_ladder=[],
                soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
                soft_kill_boundary="above",
                funding_paid_cumulative_usd=Decimal("0"),
            )

    def test_bad_direction_raises(self) -> None:
        with pytest.raises(HoldingReportError):
            compute_holding_report(
                date=dt.date(2026, 5, 24),
                instrument="SPCXUSDT-PERP.BINANCE",
                fills=[
                    _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                          price="220", reduce_only=False),
                ],
                current_mark_usd=Decimal("210"),
                meta=_meta_spcx(),
                hwm_drawdown_pct=Decimal("0"),
                tp_ladder=[],
                soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
                soft_kill_boundary="above",
                funding_paid_cumulative_usd=Decimal("0"),
                direction="sideways",  # type: ignore[arg-type]
            )

    def test_empty_instrument_raises(self) -> None:
        with pytest.raises(HoldingReportError):
            compute_holding_report(
                date=dt.date(2026, 5, 24),
                instrument="",
                fills=[
                    _fill(ts="2026-05-24T10:00:00Z", side="SELL", qty="1",
                          price="220", reduce_only=False),
                ],
                current_mark_usd=Decimal("210"),
                meta=_meta_spcx(),
                hwm_drawdown_pct=Decimal("0"),
                tp_ladder=[],
                soft_kill_trigger_mcap_usd=Decimal("3500000000000"),
                soft_kill_boundary="above",
                funding_paid_cumulative_usd=Decimal("0"),
            )
