"""Full-rate CAN from rlogs (device copies only): braking — regen vs friction, and EPS steering faults.

Rivian R1 only: the signals come from the Rivian DBC. Other cars' rlogs are skipped (see BRANDS).

The comma's harness doesn't see the motor-torque or wheel-pressure messages, so regen is inferred:
the car is slowing (IMU) with no master-cylinder pressure → the motors are doing it.
"""
import cantools
from influxdb_client import Point, WritePrecision

from .config import ROOT
from .extract import read_events
from .engage import EngagementTracker, alert_cause
from .faults import FaultTracker

BRANDS = {"rivian"}
_db = cantools.database.load_file(str(ROOT / "schema" / "rivian_primary_actuator.dbc"), strict=False)

# bus 0 = primary actuator CAN; 128 = what openpilot sent
SIGNALS = {
  (0, "RCM_IMU_LongAcc"): {"RCM_IMU_LongAcc_Sig": "accel"},
  (0, "ESPiB3"): {"ESPiB3_pMC1": "brake_pressure", "ESPiB3_ForceBlendingActive": "force_blending"},
  (0, "iBESP1"): {"iBESP1_sOutputRodDriver": "brake_pedal_mm"},
  (0, "iBESP2"): {"iBESP2_BrakePedalApplied": "brake_applied"},
  (0, "VDM_PropStatus"): {"VDM_AcceleratorPedalPosition": "accel_pedal", "VDM_VehicleSpeed": "speed_kph"},
  (0, "VDM_OutputSignals"): {"VDM_BrakeLightRequest": "brake_light"},
  (128, "ACM_longitudinalRequest"): {"ACM_AccelerationRequest": "op_accel_request"},
}
_IDS = {(bus, _db.get_message_by_name(name).frame_id): sigs for (bus, name), sigs in SIGNALS.items()}
# EPS status, decoded at full rate for faults
_EPS_STATUS = _db.get_message_by_name("EPAS_SystemStatus")
_EPS_ADAS = _db.get_message_by_name("EPAS_AdasStatus")
EAC_FAULT = 4  # EPAS_EacStatus: angle controller permanently faulted
# the driver's stalk, as the VDM reports it on bus 0: pushed up (1, 2) = cancel
_VDM_ADAS = _db.get_message_by_name("VDM_AdasSts")
STALK_UP = (1, 2)
CAUSE_WINDOW_NS = 1.0e9  # a stalk push / openpilot cancel this long before the end caused it

# numeric so a time window can take the max: friction beats regen beats nothing
DECEL_CODES = {"": 0, "hold": 1, "regen": 2, "friction": 3}
DECEL = -0.3          # m/s²: slower than this counts as braking
PRESSURE = 1.0        # bar of master-cylinder pressure = friction brakes working
MOVING_KPH = 3.0


def decel_source(v: dict) -> str:
  moving = v.get("speed_kph", 0) > MOVING_KPH
  pressed = v.get("brake_pressure", 0) > PRESSURE
  if not moving:
    return "hold" if pressed else ""
  if v.get("accel", 0) < DECEL:
    return "friction" if pressed else "regen"
  return ""


def extract_rlog(path, tags: dict, fallback_start_ns: int | None) -> tuple[list[Point], dict]:
  msgs = read_events(path)
  offset = None
  cur: dict = {}
  rows: list[tuple[int, dict]] = []
  eps: list[tuple[int, bool, bool]] = []  # (t, ToiFlt, other EPS fault)
  eac_fault = False
  first = None
  last = None
  last_emit = 0
  # engagement transitions (t, engaged, speed, cause) with what they need decided now; replayed once the clock is known
  transitions: list[tuple[int, bool, float, str | None]] = []
  engaged = None
  v_ego, brake_t, stalk_up_t, op_cancel_t = 0.0, 0, 0, 0
  disables: list[tuple[int, str, str]] = []

  def rlog_cause(t: int) -> str:
    alert = alert_cause(disables, t)
    if alert:
      return alert
    if t - brake_t < 0.5e9:
      return "brake"
    if t - stalk_up_t < CAUSE_WINDOW_NS:
      return "stalk cancel"
    if t - op_cancel_t < CAUSE_WINDOW_NS:
      return "openpilot cancel"
    return "car dropped ACC"

  for m in msgs:
    w = m.which()
    t = m.logMonoTime
    if w != "initData":  # replayed at the top of every segment with the boot's time
      first = t if first is None else first
      last = t
    if w == "gpsLocationExternal" and offset is None:
      g = m.gpsLocationExternal
      if g.hasFix and g.unixTimestampMillis > 0:
        offset = g.unixTimestampMillis * 1_000_000 - t
    elif w == "carParams" and m.carParams.brand not in BRANDS:
      return [], {}
    elif w == "carState":
      v_ego = m.carState.vEgo
      if m.carState.brakePressed:
        brake_t = t
    elif w == "carControl":
      if m.carControl.cruiseControl.cancel:
        op_cancel_t = t
    elif w == "onroadEvents":
      disables[:] = [d for d in disables if t - d[0] < 2e9]
      for e in m.onroadEvents:
        if e.softDisable or e.immediateDisable or e.userDisable:
          disables.append((t, str(e.name), "soft" if e.softDisable else "immediate" if e.immediateDisable else "user"))
    elif w == "selfdriveState":
      en = m.selfdriveState.enabled
      if en != engaged:
        transitions.append((t, en, v_ego, rlog_cause(t) if engaged and not en else None))
        engaged = en
    elif w == "can":
      for c in m.can:
        if c.src == 0 and c.address == _VDM_ADAS.frame_id:
          if _VDM_ADAS.decode(bytes(c.dat), decode_choices=False)["VDM_UserAdasRequest"] in STALK_UP:
            stalk_up_t = t
        elif c.src == 0 and c.address == _EPS_ADAS.frame_id:
          eac_fault = _EPS_ADAS.decode(bytes(c.dat), decode_choices=False)["EPAS_EacStatus"] == EAC_FAULT
        elif c.src == 0 and c.address == _EPS_STATUS.frame_id:
          d = _EPS_STATUS.decode(bytes(c.dat), decode_choices=False)
          eps.append((t, d["H_CAN_EPSS_ToiFlt"] == 1, d["EPAS_SteeringFault"] == 1 or eac_fault))
        sigs = _IDS.get((c.src, c.address))
        if sigs:
          d = _db.decode_message(c.address, bytes(c.dat), decode_choices=False)
          for s, field in sigs.items():
            cur[field] = float(d[s])
      if t - last_emit >= 100_000_000 and "accel" in cur and "speed_kph" in cur:  # 10 Hz, like the qlog data
        last_emit = t
        rows.append((t, dict(cur)))
  if offset is None:
    if fallback_start_ns is None or first is None:
      return [], {}
    offset = fallback_start_ns - first

  points = []
  summary = {"regen_s": 0.0, "friction_s": 0.0, "hold_s": 0.0, "peak_regen": 0.0}
  prev = None
  for t, v in rows:
    src = decel_source(v)
    dt = min((t - prev) / 1e9, 1.0) if prev else 0.0
    prev = t
    if src:
      summary[f"{src}_s"] += dt
    if src == "regen":
      summary["peak_regen"] = min(summary["peak_regen"], v["accel"])
    p = (Point("braking").time(t + offset, WritePrecision.NS).field("decel_source", src)
         .field("decel_code", DECEL_CODES[src]))
    for k, val in v.items():
      p = p.field(k, round(val, 3))
    for k, val in tags.items():
      p = p.tag(k, val)
    points.append(p)
  faults = FaultTracker(tags, offset)
  for t, toi, other in eps:
    faults.update("eps_torque_fault", t, toi)
    faults.update("eps_fault", t, other)
  points += faults.finish(eps[-1][0] if eps else None)
  # engagements: replace the qlog's guesses for this segment (sync.ingest_rlogs deletes them over summary["span"])
  def pt(meas: str, t: int) -> Point:
    p = Point(meas).time(t + offset, WritePrecision.NS)
    for k, val in tags.items():
      p = p.tag(k, val)
    return p
  eng_faults = FaultTracker(tags, offset)
  engagements = EngagementTracker(pt, eng_faults, "rlog")
  for t, en, v, cause in transitions:
    engagements.update(t, en, v, lambda: cause)
  points += engagements.points + eng_faults.points
  if rows:
    s = Point("braking_summary").time(rows[0][0] + offset, WritePrecision.NS)
    for k, val in tags.items():
      s = s.tag(k, val)
    for k, val in summary.items():
      s = s.field(k, round(val, 3))
    points.append(s)
  summary["span"] = (first + offset, last + offset) if first is not None else None
  return points, summary
