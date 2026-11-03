"""Agent clocks.  Time is in seconds after the incident's `created_at`."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def iso_at(base_iso: str, t_s: float) -> str:
    dt = parse_iso(base_iso) + timedelta(seconds=float(t_s))
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def seconds_between(base_iso: str, iso: str) -> float:
    return (parse_iso(iso) - parse_iso(base_iso)).total_seconds()


class ReplayClock:
    """Simulated time: waits advance instantly."""

    def __init__(self, t0: float = 0.0):
        self.t = t0

    def now(self) -> float:
        return self.t

    def advance_to(self, t_s: float) -> float:
        self.t = max(self.t, t_s)
        return self.t


class PacedClock(ReplayClock):
    """Replay paced against the wall clock (speed x real time), for live demos."""

    def __init__(self, t0: float = 0.0, speed: float = 10.0):
        super().__init__(t0)
        self.speed = max(speed, 1e-3)

    def advance_to(self, t_s: float) -> float:
        if t_s > self.t:
            time.sleep(min((t_s - self.t) / self.speed, 30.0))
        return super().advance_to(t_s)
