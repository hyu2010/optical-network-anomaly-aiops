"""Delta consumers — Design/10 §8.

Each subscriber implements `async def on_delta(delta, event) -> None`.
R-1 ships only the event log; WebSocket / rule engine / query API are R-3+.
"""
from .csv_mirror import CsvMirrorSubscriber
from .event_log import EventLogSubscriber
from .rule_engine import RuleEngineSubscriber
from .sse import SSESubscriber

__all__ = ["CsvMirrorSubscriber", "EventLogSubscriber",
           "RuleEngineSubscriber", "SSESubscriber"]
