"""Built-in `SignalDrivenStrategy` plugins.

Importing this package registers every shipped plugin in the global
strategy registry (`xtrade.strategy.base._STRATEGY_REGISTRY`) so the
CLI's `xtrade strategy list` can discover them without bespoke
configuration.
"""

from xtrade.strategy.plugins import mcap_anchored_ladder  # noqa: F401
from xtrade.strategy.plugins import momentum_follow  # noqa: F401

__all__: list[str] = []
