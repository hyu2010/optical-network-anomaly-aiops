"""Adapters — Design/10 §3, §11.

R-1 ships only the simulator adapter (tails `data/incidents.db`). Production
adapters (NETCONF, SNMP, Kafka, YANG file watcher) are R-2 / R-5 work.
"""
from .simulator import SimulatorAdapter
from .yang_watch import YangWatchAdapter

__all__ = ["SimulatorAdapter", "YangWatchAdapter"]
