"""NETCONF adapter — Design/10 §3, §11 (R-5).

NOT IMPLEMENTED.  This file is the seam.

WHY NOT YET:
  - Requires `ncclient` (or equivalent) as a new dependency.
  - Requires knowledge of the target controller's YANG bindings and the
    notification subtree to subscribe to. That's a deployment-specific
    detail that doesn't exist in this repo.

WHAT IT SHOULD DO when implemented:

  1. Connect to a NETCONF endpoint (host / port / credentials from env or
     CLI args).
  2. Establish a notification subscription on the topology + alarms streams
     using <create-subscription> per RFC 5277, or <establish-subscription>
     per RFC 8639 for filtered subscriptions.
  3. For each notification:
       - Parse the YANG XML.
       - Map to one of our `EventKind.TOPOLOGY_*` / `EventKind.ALARM_*` kinds
         per Design/10 §4 payload schemas. Granular topology events become
         useful here — `topology.add_ne`, `topology.add_span`, etc — because
         the controller can produce them directly without re-sending the
         whole topology.
       - `await engine.submit(event)`.
  4. On disconnect: reconnect with exponential backoff, then issue
     <get-config> to resync state before resuming subscription (Design/10 §9
     "Adapter crash → adapter restart; on reconnect, request the
     controller's current snapshot to resync").

REFERENCE SHAPE: adapters/simulator.py and adapters/yang_watch.py both
implement the same `__call__(engine) -> Awaitable[None]` protocol the
engine expects; this module should follow it.
"""
from __future__ import annotations


class NetconfAdapter:
    """Placeholder. Construction raises NotImplementedError to make sure
    nobody pulls this in by accident before it's implemented.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        raise NotImplementedError(
            "NETCONF adapter is R-5 work. See src/kg_engine/adapters/netconf.py "
            "for the implementation outline and Design/10 §11 for the contract."
        )
