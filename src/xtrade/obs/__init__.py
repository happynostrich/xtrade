"""Observability helpers for Phase 4.

Per `docs/phase4_brief.md` §8: every structured log emitted by xtrade
services must be a single JSON line so `journalctl -u xtrade-* | jq`
can be the operator's primary triage tool. This package centralises
that shape so supervisor, bridge.out, bridge.in and scanner all emit
the same envelope.
"""

from xtrade.obs.log_event import emit_event, format_event

__all__ = ["emit_event", "format_event"]
