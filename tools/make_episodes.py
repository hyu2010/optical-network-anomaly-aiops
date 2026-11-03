#!/usr/bin/env python3
"""Build a warm episodic memory by replaying labelled past incidents (doc 07 §2 step 4).

Each raw incident is diagnosed cold and without an LLM.  A commit that the
labels confirm is stored as `repair_confirmed` (weight 1.0); a wrong commit
is stored as `rejected` (weight 0); IDK outcomes are not stored.

  python tools/make_episodes.py                                  # history.db -> fixtures/episodes_warm.sqlite
  python tools/make_episodes.py --db X.db --out data/diag_agent/episodes.sqlite
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diag_agent.app import Agent, RunOptions, settings_from  # noqa: E402
from diag_agent.environment.substrate.simulator_db import list_incidents  # noqa: E402
from diag_agent.episodic_memory.store import EpisodeStore  # noqa: E402
from diag_agent.harness.scorer import score  # noqa: E402
from diag_agent.procedural_memory.decision_procedure.nodes import episode_of  # noqa: E402

FIXTURES = ROOT / "tests" / "diag_agent" / "fixtures"


def build(settings, out: str, db: str, *, store: str = "/tmp/diag_agent_episodes_inc.sqlite") -> int:
    Path(out).unlink(missing_ok=True)
    Path(store).unlink(missing_ok=True)
    episodes = EpisodeStore(out)
    n = 0
    for inc in list_incidents(db, suppressed=False):
        rid = int(inc["id"])
        agent = Agent(settings, RunOptions(db=db, incident=rid, no_llm=True, cold=True, incidents_store=store,
                                           validate="off", learn=False))
        st = agent.run()
        sc = score(agent.sem, st, db, rid)
        if sc["committed"]:
            ver = "repair_confirmed" if sc["correct"] else "rejected"
            episodes.write_episode(episode_of(agent.sem, st, ver))
            n += 1
        print(f"  #{rid} {sc['truth']['fault_type']} -> {st['decision']} {sc['leader']['mode']}@{sc['leader']['location']}"
              f" {'stored ' + ver if sc['committed'] else 'not stored'}", flush=True)
        agent.close()
    episodes.close()
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(FIXTURES / "history.db"))
    ap.add_argument("--out", default=str(FIXTURES / "episodes_warm.sqlite"))
    a = ap.parse_args()
    n = build(settings_from(), a.out, a.db)
    print(f"{a.out}: {n} episodes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
