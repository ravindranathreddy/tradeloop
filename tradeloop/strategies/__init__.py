"""Strategy code. Which strategy runs, and with what parameters, comes from a config
file in strategies/ at the repo root; only approved config changes change what trades."""
from tradeloop.strategies.timed_straddle import TimedStraddle

REGISTRY = {"timed_straddle": TimedStraddle}
