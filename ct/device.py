"""Drives copied straight off the device (archive/device/<route>--<seg>/{qlog,rlog}.zst).

The comma is on switched power, so it's only reachable for a short while after parking at home.
`pull()` is cheap to call often: a port check, then (only while parked) rsync of finished segments,
qlogs first so a short window still gets the dashboard data, then rlogs. rsync resumes next time.

Routes get a route.json (from the cloud if it already wrote one, else built from the logs) and their
qlogs are linked into the normal archive layout so sync.py can ingest (and --reload) them like any other.
"""
import json
import os
import socket
import subprocess
from collections import defaultdict
from pathlib import Path

from .config import ARCHIVE, ENV
from .extract import read_events
from .sources import route_dir

DEVICE = ARCHIVE / "device"
REALDATA = "/data/media/0/realdata"


def _ssh_opts() -> list[str]:
  key = Path(ENV.get("SSH_KEY", "/secrets/comma_ssh_key"))
  return [*(["-i", str(key)] if key.exists() else []), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new",
          "-o", f"UserKnownHostsFile={ARCHIVE / 'known_hosts'}", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2"]


def _host() -> str:
  return f"comma@{ENV['DEVICE_IP']}"


def reachable() -> bool:
  try:
    with socket.create_connection((ENV["DEVICE_IP"], 22), timeout=3):
      return True
  except OSError:
    return False


def _ssh(cmd: str, timeout: int = 30) -> str:
  return subprocess.run(["ssh", *_ssh_opts(), _host(), cmd], capture_output=True, text=True, timeout=timeout, check=True).stdout


def finished_segments(now: bool = False) -> list[str] | None:
  """Segment dirs safe to copy, or None if the car isn't parked (never copy during a drive).
  now: copy even with the car on. A segment still being written has a .lock file, so it's skipped either way."""
  parked = "" if now else '[ "$(cat /data/params/d/IsOffroad 2>/dev/null)" = "1" ] || exit 0; '
  out = _ssh(parked + f'echo PARKED; cd {REALDATA} && '
             'for d in *--*--*; do ls "$d"/*.lock >/dev/null 2>&1 || echo "$d"; done')
  lines = out.split()
  if not lines or lines[0] != "PARKED":
    return None
  return lines[1:]


def pull(log, now: bool = False, route: str | None = None) -> int:
  """Copy finished segments' logs (only `route`'s, if given). Returns the number of files transferred."""
  segs = finished_segments(now)
  if segs is None:
    log("device: reachable but not parked, skipping")
    return 0
  if route:
    segs = [s for s in segs if s.startswith(route)]
    log(f"device: {len(segs)} finished segments of {route} on the device")
  DEVICE.mkdir(parents=True, exist_ok=True)
  total = 0
  for name in ("qlog.zst", "rlog.zst"):
    files = "\n".join(f"{s}/{name}" for s in segs) + "\n"
    # partial files wait in .rsync-partial, so a half-copied rlog never looks complete
    p = subprocess.run(["rsync", "-a", "--partial-dir=.rsync-partial", "--ignore-missing-args", "--out-format=%n", "--files-from=-",
                        "-e", "ssh " + " ".join(_ssh_opts()), f"{_host()}:{REALDATA}/", f"{DEVICE}/"],
                       input=files, capture_output=True, text=True, timeout=3600)
    copied = [ln for ln in p.stdout.splitlines() if ln.endswith(".zst")]
    total += len(copied)
    if p.returncode != 0:
      log(f"device: rsync {name} stopped ({p.returncode}): {p.stderr.strip()[-200:]}")
      break  # probably powered off; resume next time
  if total:
    log(f"device: copied {total} files")
  return total


def device_routes() -> dict[str, list[int]]:
  routes = defaultdict(list)
  for d in DEVICE.glob("*--*--*"):
    if (d / "qlog.zst").exists():
      route, seg = d.name.rsplit("--", 1)
      routes[route].append(int(seg))
  return {r: sorted(s) for r, s in sorted(routes.items())}


def _scan(path):
  """First/last logMonoTime, a GPS (mono, unix ms) pair, and initData from one qlog."""
  first = last = gps = None
  init = {}
  for m in read_events(path):
    t = m.logMonoTime
    w = m.which()
    if w != "initData":  # replayed at the top of every segment with the boot's time
      first = t if first is None else first
    last = t
    if w == "gpsLocationExternal" and gps is None:
      g = m.gpsLocationExternal
      if g.hasFix and g.unixTimestampMillis > 0:
        gps = (t, g.unixTimestampMillis)
    elif w == "initData" and not init:
      init = {"git_branch": m.initData.gitBranch, "git_commit": m.initData.gitCommit}
  return first, last, gps, init


def build_route(route: str, segs: list[int]) -> dict | None:
  """Cloud-style route dict from the logs. None if no segment ever got a GPS time fix."""
  scans = {s: _scan(DEVICE / f"{route}--{s}" / "qlog.zst") for s in segs}
  gps = next((sc[2] for sc in scans.values() if sc[2]), None)
  if gps is None:
    return None
  offset_ms = gps[1] - gps[0] / 1e6  # mono clock is continuous across a route's segments
  init = next((sc[3] for sc in scans.values() if sc[3]), {})
  starts = [int(scans[s][0] / 1e6 + offset_ms) for s in segs]
  return {
    "fullname": f"{ENV['DONGLE_ID']}|{route}",
    "start_time_utc_millis": starts[0],
    "end_time_utc_millis": int(scans[segs[-1]][1] / 1e6 + offset_ms),
    "segment_numbers": segs,
    "segment_start_times": starts,
    "distance": None,  # summed from the logs during ingest
    "source": "device",
    **init,
  }


def stage(route: str, segs: list[int]) -> dict | None:
  """Link qlogs into archive/<dongle>_<route>/ and return its route dict (cloud's if present, else built)."""
  name = f"{ENV['DONGLE_ID']}|{route}"
  d = route_dir(name)
  meta = d / "route.json"
  r = json.loads(meta.read_text()) if meta.exists() else None
  if r is None or r.get("source") == "device":
    built = build_route(route, segs)
    if built is None:
      return None
    if r:  # keep the running distance total across pulls
      built["log_metres"] = r.get("log_metres", 0.0)
    r = built
  d.mkdir(parents=True, exist_ok=True)
  for s in segs:
    link = d / f"{s}.qlog.zst"
    if not link.exists():
      os.symlink(os.path.relpath(DEVICE / f"{route}--{s}" / "qlog.zst", d), link)
  meta.write_text(json.dumps(r, indent=1))
  return r
