"""Snapshot + restore for the StateStore — Design/10 §6.3.

Pickle the in-memory `StateStore` to disk every N seconds. On startup, load
the newest snapshot and replay any events in the NDJSON log whose
`event_id > state.last_event_id`. This bounds recovery time to one
snapshot interval (60 s default).

File naming: `data/kg_engine/snapshots/snap_<ulid>.pkl` — the ULID-ish
filename embeds the wall-clock time so `sorted(...)` picks the newest.
Older snapshots are pruned to keep the directory bounded.
"""
from __future__ import annotations

import os
import pickle
from typing import Optional

from .events import new_event_id
from .state import StateStore


def snapshot(state: StateStore, snapshots_dir: str) -> str:
    """Persist `state` to a new file under `snapshots_dir`. Return its path."""
    os.makedirs(snapshots_dir, exist_ok=True)
    path = os.path.join(snapshots_dir, f"snap_{new_event_id()}.pkl")
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        pickle.dump(state, f, protocol=pickle.HIGHEST_PROTOCOL)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path


def newest_snapshot_path(snapshots_dir: str) -> Optional[str]:
    if not os.path.isdir(snapshots_dir):
        return None
    files = sorted(f for f in os.listdir(snapshots_dir)
                   if f.startswith("snap_") and f.endswith(".pkl"))
    if not files:
        return None
    return os.path.join(snapshots_dir, files[-1])


def restore(snapshots_dir: str) -> Optional[StateStore]:
    """Load the newest snapshot; return None if there is none."""
    path = newest_snapshot_path(snapshots_dir)
    if path is None:
        return None
    with open(path, "rb") as f:
        return pickle.load(f)


def prune(snapshots_dir: str, keep: int = 3) -> int:
    """Keep the newest `keep` snapshots; delete the rest. Return delete count."""
    if not os.path.isdir(snapshots_dir):
        return 0
    files = sorted(f for f in os.listdir(snapshots_dir)
                   if f.startswith("snap_") and f.endswith(".pkl"))
    to_delete = files[:-keep] if len(files) > keep else []
    for f in to_delete:
        try:
            os.unlink(os.path.join(snapshots_dir, f))
        except OSError:
            pass
    return len(to_delete)
