#!/usr/bin/env bash
# 01_daily_holding_report.sh — Phase 6 T11 thin wrapper.
#
# Per docs/phase6_brief.md §5 T11, this is the daily cron entry point:
#
#   xtrade ops holding_report <UTC date>
#       --current-mark <decimal>
#       --instrument SPCXUSDT-PERP.BINANCE
#       --soft-kill-trigger-mcap-usd 3500000000000
#       --soft-kill-boundary above
#       --meta-yaml /etc/xtrade/instrument_meta.yaml
#       --fills-jsonl /var/lib/xtrade/state/fills.jsonl
#       --tp-ladder-json /var/lib/xtrade/state/tp_ladder.json
#       --drawdown-state-json /var/lib/xtrade/state/drawdown.json
#       --funding-paid-cumulative <decimal>
#       --output-dir /var/lib/xtrade/reports/phase6
#
# Inputs come from env (sourced from /etc/xtrade/env on the VPS):
#   XTRADE_INSTRUMENT             default SPCXUSDT-PERP.BINANCE
#   XTRADE_CURRENT_MARK_USD       REQUIRED — operator enters today's
#                                 mark (or sourced via a venue ping
#                                 script the operator runs first)
#   XTRADE_SOFT_KILL_TRIGGER      default 3500000000000
#   XTRADE_SOFT_KILL_BOUNDARY     default above
#   XTRADE_FUNDING_PAID_CUM_USD   default 0 (manual until automated)
#   XTRADE_META_YAML              default /etc/xtrade/instrument_meta.yaml
#   XTRADE_FILLS_JSONL            default /var/lib/xtrade/state/fills.jsonl
#   XTRADE_TP_LADDER_JSON         default /var/lib/xtrade/state/tp_ladder.json
#   XTRADE_DRAWDOWN_STATE_JSON    default /var/lib/xtrade/state/drawdown.json
#   XTRADE_REPORTS_DIR            default /var/lib/xtrade/reports/phase6
#   XTRADE_DATE                   default $(date -u +%Y-%m-%d)
#
# Exit codes:
#   0  report written + (best-effort) alert dispatched
#   2  config / precondition error (bad date, missing meta, etc.)
#
# Brief §5 T11.8 (runbook): on IPO event day run this hourly instead
# of daily; pass `--date YYYY-MM-DD` overrides via XTRADE_DATE.

set -euo pipefail

XTRADE_INSTRUMENT="${XTRADE_INSTRUMENT:-SPCXUSDT-PERP.BINANCE}"
XTRADE_SOFT_KILL_TRIGGER="${XTRADE_SOFT_KILL_TRIGGER:-3500000000000}"
XTRADE_SOFT_KILL_BOUNDARY="${XTRADE_SOFT_KILL_BOUNDARY:-above}"
XTRADE_FUNDING_PAID_CUM_USD="${XTRADE_FUNDING_PAID_CUM_USD:-0}"
XTRADE_META_YAML="${XTRADE_META_YAML:-/etc/xtrade/instrument_meta.yaml}"
XTRADE_FILLS_JSONL="${XTRADE_FILLS_JSONL:-/var/lib/xtrade/state/fills.jsonl}"
XTRADE_TP_LADDER_JSON="${XTRADE_TP_LADDER_JSON:-/var/lib/xtrade/state/tp_ladder.json}"
XTRADE_DRAWDOWN_STATE_JSON="${XTRADE_DRAWDOWN_STATE_JSON:-/var/lib/xtrade/state/drawdown.json}"
XTRADE_REPORTS_DIR="${XTRADE_REPORTS_DIR:-/var/lib/xtrade/reports/phase6}"
XTRADE_DATE="${XTRADE_DATE:-$(date -u +%Y-%m-%d)}"

if [[ -z "${XTRADE_CURRENT_MARK_USD:-}" ]]; then
  echo "01_daily_holding_report.sh: XTRADE_CURRENT_MARK_USD is required" >&2
  echo "  (operator enters today's snapshot mark price; future versions" >&2
  echo "   will sniff this from venue REST.)" >&2
  exit 2
fi

exec xtrade ops holding_report "${XTRADE_DATE}" \
  --instrument "${XTRADE_INSTRUMENT}" \
  --current-mark "${XTRADE_CURRENT_MARK_USD}" \
  --soft-kill-trigger-mcap-usd "${XTRADE_SOFT_KILL_TRIGGER}" \
  --soft-kill-boundary "${XTRADE_SOFT_KILL_BOUNDARY}" \
  --funding-paid-cumulative "${XTRADE_FUNDING_PAID_CUM_USD}" \
  --meta-yaml "${XTRADE_META_YAML}" \
  --fills-jsonl "${XTRADE_FILLS_JSONL}" \
  --tp-ladder-json "${XTRADE_TP_LADDER_JSON}" \
  --drawdown-state-json "${XTRADE_DRAWDOWN_STATE_JSON}" \
  --output-dir "${XTRADE_REPORTS_DIR}"
