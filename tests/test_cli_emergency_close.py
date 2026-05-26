"""Tests for Phase 6 Task T10 — emergency_close shared runner + CLI.

Covers brief §5 T10 acceptance:

- httpx MockTransport 4 paths: 200, 401, 429, 5xx
- ``--side`` two paths: ``reduce-only-tp-only`` (default) + ``all``
- ``reduce-only-tp-only`` filter: reduceOnly==False rows are **not**
  cancelled (limit entry orders preserved)
- ``--yes`` guard: refuses to run without ``--yes``
- Sentinel is written **before** any HTTP call (visible even on venue
  outage)
- Crit alert dispatched on both success and failure (best-effort)
- HMAC-SHA256 signature is appended to every signed request

The signing assertion validates the canonical Binance USDT-M futures
flow:

  query = urlencode({"symbol": ..., "recvWindow": 5000, "timestamp": <ms>})
  signature = HMAC-SHA256(api_secret, query).hexdigest()
  full = query + "&signature=" + signature
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from typer.testing import CliRunner

from xtrade.cli import app
from xtrade.live.sentinel import Sentinel
from xtrade.ops import emergency_close as ec_module
from xtrade.ops.emergency_close import (
    EmergencyCloseConfigError,
    _binance_host,
    _binance_symbol,
    _build_signed_query,
    _is_reduce_only_tp,
    _sign,
    run,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

runner = CliRunner()

API_KEY = "test-api-key-aaaa"
API_SECRET = "test-api-secret-bbbb"


def _venues_yaml(tmp_path: Path) -> Path:
    yml = tmp_path / "venues.yaml"
    yml.write_text(
        "binance:\n"
        "  environment: LIVE\n"
        "  futures:\n"
        "    api_key_env: XTEST_BINANCE_API_KEY\n"
        "    api_secret_env: XTEST_BINANCE_API_SECRET\n"
        "    key_type: HMAC\n"
        "    account_type: USDT_FUTURE\n",
        encoding="utf-8",
    )
    return yml


@pytest.fixture(autouse=True)
def _set_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XTEST_BINANCE_API_KEY", API_KEY)
    monkeypatch.setenv("XTEST_BINANCE_API_SECRET", API_SECRET)


class _Handler:
    """Reusable MockTransport handler that records every request and
    routes by URL path. Lets each test wire its own responses."""

    def __init__(
        self,
        *,
        open_orders: list[dict[str, Any]] | int = None,  # type: ignore[assignment]
        cancel_one_status: int = 200,
        cancel_all_status: int = 200,
    ) -> None:
        self.calls: list[httpx.Request] = []
        self._open_orders = open_orders
        self._cancel_one_status = cancel_one_status
        self._cancel_all_status = cancel_all_status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        path = request.url.path
        if path == "/fapi/v1/openOrders":
            if isinstance(self._open_orders, int):
                return httpx.Response(self._open_orders, text="venue error")
            return httpx.Response(200, json=self._open_orders or [])
        if path == "/fapi/v1/order":
            if self._cancel_one_status == 200:
                return httpx.Response(200, json={"orderId": 1, "status": "CANCELED"})
            return httpx.Response(self._cancel_one_status, text="venue error")
        if path == "/fapi/v1/allOpenOrders":
            if self._cancel_all_status == 200:
                return httpx.Response(200, json=[{"orderId": 1, "status": "CANCELED"}])
            return httpx.Response(self._cancel_all_status, text="venue error")
        return httpx.Response(404, text="unexpected route")


class _FakeAlerter:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    def dispatch_alert(self, **kwargs: Any) -> None:
        self.dispatched.append(kwargs)

    def close(self) -> None:
        pass


def _order_row(order_id: int, *, reduce_only: bool, otype: str = "LIMIT") -> dict[str, Any]:
    return {
        "orderId": order_id,
        "symbol": "SPCXUSDT",
        "type": otype,
        "side": "BUY" if reduce_only else "SELL",
        "reduceOnly": reduce_only,
        "status": "NEW",
    }


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_binance_symbol_from_nautilus(self) -> None:
        assert _binance_symbol("SPCXUSDT-PERP.BINANCE") == "SPCXUSDT"

    def test_binance_symbol_bare(self) -> None:
        assert _binance_symbol("SPCXUSDT") == "SPCXUSDT"

    def test_binance_symbol_lowercased(self) -> None:
        assert _binance_symbol("spcxusdt-perp.binance") == "SPCXUSDT"

    def test_binance_symbol_rejects_empty(self) -> None:
        with pytest.raises(EmergencyCloseConfigError):
            _binance_symbol("")

    def test_binance_symbol_rejects_garbage(self) -> None:
        with pytest.raises(EmergencyCloseConfigError):
            _binance_symbol("-PERP.BINANCE")

    def test_binance_host_live(self) -> None:
        assert _binance_host("LIVE") == "https://fapi.binance.com"

    def test_binance_host_testnet(self) -> None:
        assert _binance_host("TESTNET") == "https://testnet.binancefuture.com"

    def test_binance_host_unknown_raises(self) -> None:
        with pytest.raises(EmergencyCloseConfigError):
            _binance_host("MARS")

    def test_sign_matches_hmac_sha256(self) -> None:
        expected = hmac.new(b"sekret", b"k=v&x=y", hashlib.sha256).hexdigest()
        assert _sign("sekret", "k=v&x=y") == expected

    def test_build_signed_query_round_trip(self) -> None:
        q = _build_signed_query({"symbol": "SPCXUSDT"}, secret="sekret", now_ms=1700000000000)
        parts = dict(urllib.parse.parse_qsl(q))
        assert parts["symbol"] == "SPCXUSDT"
        assert parts["recvWindow"] == "5000"
        assert parts["timestamp"] == "1700000000000"
        # Signature is HMAC of the *pre-signature* query
        pre = urllib.parse.urlencode(
            {"symbol": "SPCXUSDT", "recvWindow": 5000, "timestamp": 1700000000000}
        )
        expected_sig = hmac.new(b"sekret", pre.encode(), hashlib.sha256).hexdigest()
        assert parts["signature"] == expected_sig

    def test_is_reduce_only_tp_bool_true(self) -> None:
        assert _is_reduce_only_tp(_order_row(1, reduce_only=True))

    def test_is_reduce_only_tp_bool_false(self) -> None:
        assert not _is_reduce_only_tp(_order_row(1, reduce_only=False))

    def test_is_reduce_only_tp_str_true(self) -> None:
        assert _is_reduce_only_tp({"reduceOnly": "true"})

    def test_is_reduce_only_tp_missing(self) -> None:
        assert not _is_reduce_only_tp({})


# ---------------------------------------------------------------------------
# Unit tests: run(...) — happy paths
# ---------------------------------------------------------------------------


class TestRunHappyPaths:
    def test_side_all_200(self, tmp_path: Path) -> None:
        handler = _Handler(cancel_all_status=200)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        alerter = _FakeAlerter()
        sentinel_path = tmp_path / "paused.flag"
        rc = run(
            side="all",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=sentinel_path,
            alerter=alerter,  # type: ignore[arg-type]
            http_client=client,
        )
        assert rc == 0
        # Sentinel written
        assert sentinel_path.exists()
        body = json.loads(sentinel_path.read_text(encoding="utf-8"))
        assert body["reason"] == "ops.emergency_close:all:SPCXUSDT-PERP.BINANCE"
        # Single DELETE /fapi/v1/allOpenOrders
        assert len(handler.calls) == 1
        req = handler.calls[0]
        assert req.method == "DELETE"
        assert req.url.path == "/fapi/v1/allOpenOrders"
        assert req.headers.get("X-MBX-APIKEY") == API_KEY
        # Signature appended
        params = dict(urllib.parse.parse_qsl(req.url.query.decode()))
        assert "signature" in params
        assert params["symbol"] == "SPCXUSDT"
        # Alert dispatched with crit severity
        assert len(alerter.dispatched) == 1
        assert alerter.dispatched[0]["severity"] == "crit"
        assert alerter.dispatched[0]["event"] == "ops.emergency_close.invoked"
        assert alerter.dispatched[0]["fields"]["exit_code"] == 0

    def test_side_reduce_only_cancels_only_reduce_only(self, tmp_path: Path) -> None:
        rows = [
            _order_row(1001, reduce_only=False, otype="LIMIT"),  # entry — preserve
            _order_row(2001, reduce_only=True, otype="LIMIT"),   # TP ladder rung
            _order_row(2002, reduce_only=True, otype="LIMIT"),
            _order_row(1002, reduce_only=False, otype="LIMIT"),  # second entry
        ]
        handler = _Handler(open_orders=rows, cancel_one_status=200)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        alerter = _FakeAlerter()
        sentinel_path = tmp_path / "paused.flag"
        rc = run(
            side="reduce-only-tp-only",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=sentinel_path,
            alerter=alerter,  # type: ignore[arg-type]
            http_client=client,
        )
        assert rc == 0
        # Sentinel written with side embedded in reason
        assert sentinel_path.exists()
        body = json.loads(sentinel_path.read_text(encoding="utf-8"))
        assert body["reason"] == "ops.emergency_close:reduce-only-tp-only:SPCXUSDT-PERP.BINANCE"
        # GET openOrders + 2x DELETE /fapi/v1/order (NOT 4 — entries preserved)
        methods_paths = [(c.method, c.url.path) for c in handler.calls]
        assert methods_paths[0] == ("GET", "/fapi/v1/openOrders")
        cancels = [c for c in handler.calls if c.url.path == "/fapi/v1/order"]
        assert len(cancels) == 2
        cancelled_ids = sorted(
            int(dict(urllib.parse.parse_qsl(c.url.query.decode()))["orderId"])
            for c in cancels
        )
        assert cancelled_ids == [2001, 2002]
        # Crit alert with cancelled=2 / failures=0
        assert alerter.dispatched[0]["fields"]["cancelled"] == 2
        assert alerter.dispatched[0]["fields"]["failures"] == 0
        assert alerter.dispatched[0]["fields"]["listed"] == 4

    def test_side_reduce_only_with_no_reduce_only_rows_is_zero(self, tmp_path: Path) -> None:
        rows = [
            _order_row(1001, reduce_only=False),
            _order_row(1002, reduce_only=False),
        ]
        handler = _Handler(open_orders=rows)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        alerter = _FakeAlerter()
        rc = run(
            side="reduce-only-tp-only",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=alerter,  # type: ignore[arg-type]
            http_client=client,
        )
        assert rc == 0
        # Only the listing call was made
        assert len(handler.calls) == 1
        assert handler.calls[0].url.path == "/fapi/v1/openOrders"
        assert alerter.dispatched[0]["fields"]["cancelled"] == 0
        assert alerter.dispatched[0]["fields"]["failures"] == 0


# ---------------------------------------------------------------------------
# Unit tests: run(...) — HTTP failure paths (brief §5 T10: 200/401/429/5xx)
# ---------------------------------------------------------------------------


class TestRunHttpPaths:
    def test_side_all_401_returns_2(self, tmp_path: Path) -> None:
        handler = _Handler(cancel_all_status=401)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        alerter = _FakeAlerter()
        rc = run(
            side="all",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=alerter,  # type: ignore[arg-type]
            http_client=client,
        )
        assert rc == 2
        assert alerter.dispatched[0]["severity"] == "crit"
        assert alerter.dispatched[0]["fields"]["exit_code"] == 2

    def test_side_all_429_returns_2(self, tmp_path: Path) -> None:
        handler = _Handler(cancel_all_status=429)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        rc = run(
            side="all",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=None,
            http_client=client,
        )
        assert rc == 2

    def test_side_all_5xx_returns_2(self, tmp_path: Path) -> None:
        handler = _Handler(cancel_all_status=503)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        rc = run(
            side="all",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=None,
            http_client=client,
        )
        assert rc == 2

    def test_side_reduce_only_list_5xx_returns_2(self, tmp_path: Path) -> None:
        handler = _Handler(open_orders=503)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        rc = run(
            side="reduce-only-tp-only",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=None,
            http_client=client,
        )
        assert rc == 2

    def test_side_reduce_only_list_401_returns_2(self, tmp_path: Path) -> None:
        handler = _Handler(open_orders=401)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        rc = run(
            side="reduce-only-tp-only",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=None,
            http_client=client,
        )
        assert rc == 2

    def test_side_reduce_only_partial_cancel_failure_returns_2(self, tmp_path: Path) -> None:
        rows = [
            _order_row(2001, reduce_only=True),
            _order_row(2002, reduce_only=True),
        ]
        handler = _Handler(open_orders=rows, cancel_one_status=429)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        alerter = _FakeAlerter()
        rc = run(
            side="reduce-only-tp-only",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=alerter,  # type: ignore[arg-type]
            http_client=client,
        )
        assert rc == 2
        # We still attempted both cancels
        cancels = [c for c in handler.calls if c.url.path == "/fapi/v1/order"]
        assert len(cancels) == 2
        assert alerter.dispatched[0]["fields"]["failures"] == 2

    def test_sentinel_written_before_http_call(self, tmp_path: Path) -> None:
        """Even when the venue returns 5xx, the sentinel is on disk first."""
        sentinel_path = tmp_path / "paused.flag"
        observed: dict[str, bool] = {"sentinel_existed": False}

        def handler(request: httpx.Request) -> httpx.Response:
            observed["sentinel_existed"] = sentinel_path.exists()
            return httpx.Response(503, text="degraded")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        rc = run(
            side="all",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=sentinel_path,
            alerter=None,
            http_client=client,
        )
        assert rc == 2
        assert observed["sentinel_existed"] is True


# ---------------------------------------------------------------------------
# Unit tests: run(...) — config / input validation
# ---------------------------------------------------------------------------


class TestRunConfigValidation:
    def test_rejects_bad_side(self, tmp_path: Path) -> None:
        with pytest.raises(EmergencyCloseConfigError):
            run(
                side="invalid",  # type: ignore[arg-type]
                instrument="SPCXUSDT-PERP.BINANCE",
                venues_yaml=_venues_yaml(tmp_path),
                sentinel_path=tmp_path / "paused.flag",
                alerter=None,
            )

    def test_rejects_missing_venues_yaml(self, tmp_path: Path) -> None:
        with pytest.raises(EmergencyCloseConfigError):
            run(
                side="all",
                instrument="SPCXUSDT-PERP.BINANCE",
                venues_yaml=tmp_path / "nope.yaml",
                sentinel_path=tmp_path / "paused.flag",
                alerter=None,
            )

    def test_rejects_venues_yaml_without_futures(self, tmp_path: Path) -> None:
        yml = tmp_path / "spot_only.yaml"
        yml.write_text(
            "binance:\n"
            "  environment: LIVE\n"
            "  spot:\n"
            "    api_key_env: XTEST_BINANCE_API_KEY\n"
            "    api_secret_env: XTEST_BINANCE_API_SECRET\n"
            "    key_type: HMAC\n"
            "    account_type: SPOT\n",
            encoding="utf-8",
        )
        with pytest.raises(EmergencyCloseConfigError):
            run(
                side="all",
                instrument="SPCXUSDT-PERP.BINANCE",
                venues_yaml=yml,
                sentinel_path=tmp_path / "paused.flag",
                alerter=None,
            )

    def test_rejects_bad_instrument(self, tmp_path: Path) -> None:
        with pytest.raises(EmergencyCloseConfigError):
            run(
                side="all",
                instrument="",
                venues_yaml=_venues_yaml(tmp_path),
                sentinel_path=tmp_path / "paused.flag",
                alerter=None,
            )


# ---------------------------------------------------------------------------
# CLI tests: typer surface
# ---------------------------------------------------------------------------


class TestCliSurface:
    def test_refuses_without_yes(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ops",
                "emergency_close",
                "--instrument",
                "SPCXUSDT-PERP.BINANCE",
                "--venues-yaml",
                str(_venues_yaml(tmp_path)),
                "--sentinel-path",
                str(tmp_path / "paused.flag"),
                # NOTE: no --yes
            ],
        )
        assert result.exit_code == 2, result.output
        # Sentinel must NOT have been written
        assert not (tmp_path / "paused.flag").exists()

    def test_rejects_unknown_side(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ops",
                "emergency_close",
                "--yes",
                "--side",
                "burn-it-down",
                "--instrument",
                "SPCXUSDT-PERP.BINANCE",
                "--venues-yaml",
                str(_venues_yaml(tmp_path)),
                "--sentinel-path",
                str(tmp_path / "paused.flag"),
            ],
        )
        assert result.exit_code == 2, result.output

    def test_cli_happy_path_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = _Handler(cancel_all_status=200)
        recorded_client = httpx.Client(transport=httpx.MockTransport(handler))

        # Patch the runner so the CLI uses our mock transport.
        original_run = ec_module.run

        def patched_run(**kwargs: Any) -> int:
            kwargs.setdefault("http_client", recorded_client)
            return original_run(**kwargs)

        monkeypatch.setattr("xtrade.cli.run", patched_run, raising=False)
        monkeypatch.setattr(ec_module, "run", patched_run)
        # Ensure CLI's import sees the patched symbol
        from xtrade.ops import emergency_close as ec_mod_alias
        monkeypatch.setattr(ec_mod_alias, "run", patched_run)

        sentinel_path = tmp_path / "paused.flag"
        result = runner.invoke(
            app,
            [
                "ops",
                "emergency_close",
                "--yes",
                "--side",
                "all",
                "--instrument",
                "SPCXUSDT-PERP.BINANCE",
                "--venues-yaml",
                str(_venues_yaml(tmp_path)),
                "--sentinel-path",
                str(sentinel_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "emergency_close: OK" in result.output
        assert sentinel_path.exists()

    def test_cli_failure_path_returns_2(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handler = _Handler(cancel_all_status=503)
        recorded_client = httpx.Client(transport=httpx.MockTransport(handler))

        original_run = ec_module.run

        def patched_run(**kwargs: Any) -> int:
            kwargs.setdefault("http_client", recorded_client)
            return original_run(**kwargs)

        from xtrade.ops import emergency_close as ec_mod_alias
        monkeypatch.setattr(ec_mod_alias, "run", patched_run)

        sentinel_path = tmp_path / "paused.flag"
        result = runner.invoke(
            app,
            [
                "ops",
                "emergency_close",
                "--yes",
                "--side",
                "all",
                "--instrument",
                "SPCXUSDT-PERP.BINANCE",
                "--venues-yaml",
                str(_venues_yaml(tmp_path)),
                "--sentinel-path",
                str(sentinel_path),
            ],
        )
        assert result.exit_code == 2, result.output
        # Sentinel still written (operator-visible halt)
        assert sentinel_path.exists()

    def test_cli_missing_venues_yaml_exit_2(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "ops",
                "emergency_close",
                "--yes",
                "--side",
                "all",
                "--instrument",
                "SPCXUSDT-PERP.BINANCE",
                "--venues-yaml",
                str(tmp_path / "nope.yaml"),
                "--sentinel-path",
                str(tmp_path / "paused.flag"),
            ],
        )
        assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Robustness: alerter exceptions never raise out
# ---------------------------------------------------------------------------


class TestAlerterRobustness:
    def test_alerter_crash_swallowed(self, tmp_path: Path) -> None:
        handler = _Handler(cancel_all_status=200)
        client = httpx.Client(transport=httpx.MockTransport(handler))

        class CrashAlerter:
            def dispatch_alert(self, **kwargs: Any) -> None:
                raise RuntimeError("alert pipeline down")

        rc = run(
            side="all",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=CrashAlerter(),  # type: ignore[arg-type]
            http_client=client,
        )
        # Cancel still succeeded → exit 0 even though alerter died
        assert rc == 0

    def test_alerter_none_is_fine(self, tmp_path: Path) -> None:
        handler = _Handler(cancel_all_status=200)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        rc = run(
            side="all",
            instrument="SPCXUSDT-PERP.BINANCE",
            venues_yaml=_venues_yaml(tmp_path),
            sentinel_path=tmp_path / "paused.flag",
            alerter=None,
            http_client=client,
        )
        assert rc == 0
