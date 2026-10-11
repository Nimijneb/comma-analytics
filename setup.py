#!/usr/bin/env python3
"""Interactive setup: writes .env (and secrets/comma_ssh_key if you want the LAN device pull).

Safe to re-run: current values are the defaults, and generated passwords/tokens are kept.
Needs only Python 3.9+ (standard library). Then: docker compose up -d --build
"""
from __future__ import annotations

import base64
import datetime as dt
import getpass
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / ".env"
SSH_KEY = ROOT / "secrets" / "comma_ssh_key"
COMMA_API = "https://api.commadotai.com"


def load_env() -> dict:
  env = {}
  if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
      if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
  return env


def ask(prompt: str, default: str = "", hidden: bool = False) -> str:
  shown = "(keep current)" if hidden and default else default
  q = f"{prompt} [{shown}]: " if shown else f"{prompt}: "
  val = (getpass.getpass(q) if hidden and sys.stdin.isatty() else input(q)).strip()
  return val or default


def yes(prompt: str, default: bool) -> bool:
  val = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
  return default if not val else val.startswith("y")


def get_json(url: str, headers: dict) -> dict | list:
  with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
    return json.load(r)


def jwt_expiry(token: str) -> dt.datetime | None:
  try:
    payload = token.split(".")[1]
    exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["exp"]
    return dt.datetime.fromtimestamp(exp)
  except Exception:
    return None


def section(title: str) -> None:
  print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def comma(env: dict) -> None:
  section("comma account")
  print("Log in at https://jwt.comma.ai with the account your device is paired to, and copy the token.\n"
        "It lasts about 90 days; the dashboard shows when it expires. Renew the same way, then re-run this.")
  while True:
    token = ask("comma token", env.get("COMMA_TOKEN", ""), hidden=True)
    try:
      me = get_json(f"{COMMA_API}/v1/me/", {"Authorization": f"JWT {token}"})
      devices = get_json(f"{COMMA_API}/v1/me/devices/", {"Authorization": f"JWT {token}"})
      break
    except urllib.error.HTTPError as e:
      print(f"  comma API said {e.code}: the token is wrong or expired. Try again.")
      env.pop("COMMA_TOKEN", None)
  exp = jwt_expiry(token)
  print(f"  ok: {me.get('email') or me.get('username') or me.get('id')}" + (f", token valid until {exp:%Y-%m-%d}" if exp else ""))
  env["COMMA_TOKEN"] = token
  if not devices:
    sys.exit("  no devices on this account: pair the device in comma connect first")
  for i, d in enumerate(devices, 1):
    print(f"  {i}. {d['dongle_id']}  {d.get('alias') or ''}  ({d.get('device_type', '?')})")
  cur = env.get("DONGLE_ID") or devices[0]["dongle_id"]
  pick = ask("device (number or dongle id)", cur)
  env["DONGLE_ID"] = devices[int(pick) - 1]["dongle_id"] if pick.isdigit() and int(pick) <= len(devices) else pick


def local_tz() -> str:
  if os.environ.get("TZ"):
    return os.environ["TZ"]
  try:
    return os.path.realpath("/etc/localtime").split("zoneinfo/", 1)[1]
  except (IndexError, OSError):
    return "UTC"


# time zones of places that drive in miles: the US (incl. Alaska/Hawaii) and the UK
IMPERIAL_TZ = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix",
               "America/Anchorage", "America/Boise", "America/Detroit", "America/Indiana", "America/Kentucky",
               "America/Juneau", "America/Menominee", "America/Metlakatla", "America/Nome", "America/North_Dakota",
               "America/Sitka", "America/Yakutat", "America/Adak", "Pacific/Honolulu", "US/", "Europe/London")


# everyday names people type instead of an IANA zone
TZ_ALIASES = {"pacific": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
              "mountain": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver",
              "arizona": "America/Phoenix", "central": "America/Chicago", "cst": "America/Chicago",
              "cdt": "America/Chicago", "eastern": "America/New_York", "est": "America/New_York",
              "edt": "America/New_York", "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
              "uk": "Europe/London", "gmt": "Europe/London", "bst": "Europe/London", "utc": "UTC"}


def ask_tz(default: str) -> str:
  """Accepts an IANA name, a common alias (pacific, EST, ...) or part of a city name, and lists matches to pick from."""
  try:
    import zoneinfo
    zones = sorted(z for z in zoneinfo.available_timezones() if "/" in z or z == "UTC")
  except ImportError:
    zones = []
  if not zones:  # no tz database to check against (e.g. Windows without tzdata)
    return ask("time zone (IANA name, e.g. America/Los_Angeles)", default)
  by_lower = {z.lower(): z for z in zones}
  while True:
    val = ask("time zone (a city like 'los angeles', or 'pacific', 'EST', ...)", default)
    key = val.lower().strip().replace(" ", "_")
    if key in by_lower:
      return by_lower[key]
    if key in TZ_ALIASES:
      print(f"  using {TZ_ALIASES[key]}")
      return TZ_ALIASES[key]
    matches = [z for z in zones if key in z.lower()]
    if len(matches) == 1:
      print(f"  using {matches[0]}")
      return matches[0]
    if not matches:
      print(f"  no time zone matches '{val}'; try the nearest big city in your time zone")
    elif len(matches) > 20:
      print(f"  {len(matches)} time zones match '{val}'; be more specific")
    else:
      for i, z in enumerate(matches, 1):
        print(f"  {i:>2}. {z}")
      pick = input("  number (or Enter to search again): ").strip()
      if pick.isdigit() and 1 <= int(pick) <= len(matches):
        return matches[int(pick) - 1]


def default_units(tz: str) -> str:
  return "imperial" if tz.startswith(IMPERIAL_TZ) else "metric"


def port_free(port: int) -> bool:
  with socket.socket() as s:
    try:
      s.bind(("0.0.0.0", port))
      return True
    except OSError:
      return False


def free_port(start: int) -> int:
  return next((p for p in range(start, start + 100) if port_free(p)), start)


def basics(env: dict) -> None:
  section("dashboard")
  env["TZ_NAME"] = ask_tz(env.get("TZ_NAME") or local_tz())
  while True:
    units = ask("units: imperial (mi, mph) or metric (km, km/h)", env.get("UNITS") or default_units(env["TZ_NAME"])).lower()
    if units in ("imperial", "metric"):
      env["UNITS"] = units
      break
    print("  type imperial or metric")
  saved = env.get("GRAFANA_PORT")
  port = saved or str(free_port(3000))
  if not saved and port != "3000":
    print(f"  port 3000 is in use, so {port} is suggested")
  while True:
    val = ask("Grafana port on this machine", port)
    if not val.isdigit() or not 1 <= int(val) <= 65535:
      print("  type a port number between 1 and 65535")
      continue
    # the saved port is likely held by this dashboard's own Grafana, so it isn't checked
    if val != saved and not port_free(int(val)):
      port = str(free_port(int(val) + 1))
      print(f"  port {val} is in use; {port} is free")
      continue
    env["GRAFANA_PORT"] = val
    break
  env.setdefault("GRAFANA_PASSWORD", secrets.token_urlsafe(18))
  env.setdefault("INFLUX_PASSWORD", secrets.token_urlsafe(18))
  env.setdefault("INFLUX_TOKEN", secrets.token_urlsafe(48))
  env.setdefault("INFLUX_ORG", "comma")
  env.setdefault("INFLUX_BUCKET", "comma")
  env["APP_UID"], env["APP_GID"] = str(os.getuid()), str(os.getgid())


def drivers(env: dict) -> None:
  section("drivers (optional: Home Assistant)")
  print("Without Home Assistant every drive is labelled with one name. With it, a sensor whose state is the\n"
        "current driver's name (e.g. a car integration's active-profile sensor) labels each drive.")
  env["DRIVER_NAME"] = ask("driver name for drives", env.get("DRIVER_NAME", "driver"))
  if not yes("use Home Assistant for driver names?", bool(env.get("HA_URL"))):
    env["HA_URL"] = env["HA_TOKEN"] = env["HA_DRIVER_ENTITY"] = ""
    return
  print("Token: in Home Assistant, your profile → Security → Long-lived access tokens → Create token.")
  while True:
    url = ask("Home Assistant URL", env.get("HA_URL") or "http://homeassistant.local:8123").rstrip("/")
    token = ask("Home Assistant token", env.get("HA_TOKEN", ""), hidden=True)
    entity = ask("driver sensor entity id", env.get("HA_DRIVER_ENTITY", "sensor."))
    try:
      st = get_json(f"{url}/api/states/{entity}", {"Authorization": f"Bearer {token}"})
      print(f"  ok: {entity} = {st['state']!r}")
      if url.split("//")[-1].split(":")[0] in ("localhost", "127.0.0.1"):
        print("  note: inside docker, localhost is the container. Use this machine's LAN IP instead.")
      env.update(HA_URL=url, HA_TOKEN=token, HA_DRIVER_ENTITY=entity)
      return
    except (urllib.error.URLError, OSError) as e:
      print(f"  couldn't read it: {e}")
      if not yes("try again?", True):
        env["HA_URL"] = env["HA_TOKEN"] = env["HA_DRIVER_ENTITY"] = ""
        return


def key_usable(path: str) -> bool:
  """True for a private key that opens without a passphrase (the sync container can't type one)."""
  r = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", path], capture_output=True)
  return r.returncode == 0


def device(env: dict) -> None:
  section("direct copy from the device (optional)")
  print("The comma cloud keeps only qlogs (and only ~3 days without comma prime). If the device joins your\n"
        "home Wi-Fi after you park, the full logs (rlogs) can be copied over SSH too. On a Rivian R1 that adds\n"
        "regen-vs-friction braking and EPS fault detection; for other cars it just keeps a local copy.")
  if not yes("copy logs straight off the device?", bool(env.get("DEVICE_IP"))):
    env["DEVICE_IP"] = ""
    return
  env["DEVICE_IP"] = ask("device IP on your LAN (give it a DHCP reservation)", env.get("DEVICE_IP", ""))
  SSH_KEY.parent.mkdir(exist_ok=True)
  if not SSH_KEY.exists():
    found = next((k for k in map(os.path.expanduser, ("~/.ssh/id_ed25519", "~/.ssh/id_ecdsa", "~/.ssh/id_rsa"))
                  if os.path.isfile(k) and key_usable(k)), "")
    while True:
      existing = ask("path to an SSH private key the device already accepts (or 'new' to make one)", found or "new")
      if existing.lower() == "new":
        existing = ""
        break
      existing = os.path.expanduser(existing)
      if not os.path.isfile(existing):
        print(f"  no file at {existing}")
      elif not key_usable(existing):
        print("  that key has a passphrase (or isn't a private key); the sync runs unattended, so it needs one without")
      else:
        break
    if existing:
      shutil.copyfile(existing, SSH_KEY)
      SSH_KEY.chmod(0o600)
    else:
      subprocess.run(["ssh-keygen", "-t", "ed25519", "-N", "", "-C", "comma-analytics", "-f", str(SSH_KEY)], check=True,
                     stdout=subprocess.DEVNULL)
      print("\nNew key made. The device trusts the SSH keys on a GitHub account, so:\n"
            "  1. add this public key at https://github.com/settings/keys :\n"
            f"     {(SSH_KEY.with_suffix('.pub')).read_text().strip()}\n"
            "  2. on the device: Settings → Developer (or Network) → Enable SSH, then SSH Keys → Add → your GitHub username\n"
            "     (if it's already added, Remove and Add again so it fetches the new key)")
      input("Press Enter when done (or to skip testing)...")
  r = subprocess.run(["ssh", "-i", str(SSH_KEY), "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                      "-o", "StrictHostKeyChecking=accept-new", f"comma@{env['DEVICE_IP']}", "echo ok"],
                     capture_output=True, text=True)
  print("  ssh ok" if r.returncode == 0 else
        f"  ssh didn't work yet ({r.stderr.strip()[-120:] or r.returncode}). Fine if the device is off; it's tried every minute.")


ORDER = ["COMMA_TOKEN", "DONGLE_ID", "TZ_NAME", "UNITS", "GRAFANA_PORT", "GRAFANA_PASSWORD", "INFLUX_PASSWORD", "INFLUX_TOKEN",
         "INFLUX_ORG", "INFLUX_BUCKET", "APP_UID", "APP_GID", "DRIVER_NAME", "HA_URL", "HA_TOKEN", "HA_DRIVER_ENTITY",
         "DEVICE_IP", "GITHUB_REPO", "GRAFANA_ANONYMOUS"]


def write_env(env: dict) -> None:
  keys = ORDER + sorted(k for k in env if k not in ORDER)
  ENV_FILE.write_text("# written by ./setup.py (see .env.example for what each setting does)\n" +
                      "".join(f"{k}={env.get(k, '')}\n" for k in keys))
  ENV_FILE.chmod(0o600)


def main() -> int:
  env = load_env()
  env.setdefault("GRAFANA_ANONYMOUS", "false")
  comma(env)
  basics(env)
  drivers(env)
  device(env)
  write_env(env)
  (ROOT / "archive").mkdir(exist_ok=True)
  (ROOT / "secrets").mkdir(exist_ok=True)
  section("done")
  print(f"Wrote {ENV_FILE.name}. Start it with:\n\n  docker compose up -d --build\n\n"
        f"Then open http://localhost:{env['GRAFANA_PORT']}/d/comma-telemetry  (user admin, password {env['GRAFANA_PASSWORD']})\n"
        "The first sync pulls whatever the comma cloud still has; follow it with: docker compose logs -f sync")
  return 0


if __name__ == "__main__":
  try:
    sys.exit(main())
  except (KeyboardInterrupt, EOFError):
    print("\naborted; nothing written")
    sys.exit(1)
