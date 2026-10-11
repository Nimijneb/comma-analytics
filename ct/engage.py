"""Engagements: when openpilot engaged and how each one ended.

Points: measurement `engagement`, tag event=start|end and source=qlog|rlog; an end has `cause`, `speed_mph` and
`duration_s` (when the start was in the same segment). The qlog pass can only tell brake / openpilot alert / steering
switched off / "cruise turned off"; the rlog pass (Rivian CAN) splits the last into stalk cancel, car dropped ACC and
openpilot cancel, and replaces the qlog rows for that segment (see sync.ingest_rlogs).
"""
from .faults import FaultTracker

ENGAGE_SHORT_S = 3.0  # shorter than this and not ended by the driver = "didn't stick"
# causes that mean the driver ended it on purpose
DRIVER_CAUSES = {"brake", "stalk cancel", "steering switched off"}
UNKNOWN_CAUSE = "cruise turned off"  # qlog only: could be the stalk or the car
MPH = 2.23694


class EngagementTracker:
  """Feed it frames in time order: engaged state, speed, and recent hints; it emits start/end points and faults."""

  def __init__(self, pt, faults: FaultTracker, source: str):
    self.pt, self.faults, self.source = pt, faults, source
    self.prev = None    # unknown until the first frame of the segment (no start/end emitted for it)
    self.start = None   # start time, if the engagement began in this segment
    self.points = []

  def update(self, t: int, engaged: bool, v: float, cause_fn) -> None:
    if self.prev is None:
      self.prev = engaged
      return
    if engaged and not self.prev:
      self.start = t
      self.points.append(self.pt("engagement", t).tag("event", "start").tag("source", self.source)
                         .field("speed_mph", round(v * MPH, 1)))
    elif self.prev and not engaged:
      cause = cause_fn()
      p = (self.pt("engagement", t).tag("event", "end").tag("source", self.source)
           .field("cause", cause).field("speed_mph", round(v * MPH, 1)))
      if self.start is not None:
        held = (t - self.start) / 1e9
        p = p.field("duration_s", round(held, 2))
        if held < ENGAGE_SHORT_S and cause not in DRIVER_CAUSES and cause != UNKNOWN_CAUSE:
          self.faults.add("engage_dropped", self.start, t, cause=cause)
      self.points.append(p)
      self.start = None
    self.prev = engaged


def alert_cause(recent_disables: list[tuple[int, str, str]], t: int, window_ns: float = 1.5e9) -> str | None:
  """An openpilot alert that disengaged, or the steering being switched off, from (time, name, kind) events."""
  names = [(n, k) for tt, n, k in recent_disables if t - tt <= window_ns]
  for n, k in names:
    if k in ("soft", "immediate"):
      return f"openpilot: {n}"
  if any(n in ("lkasDisable", "silentLkasDisable") for n, _ in names):
    return "steering switched off"
  return None
