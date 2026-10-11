"""Settings come from the environment (docker compose passes .env in); a local .env is read too when running outside docker."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE = Path(os.environ.get("ARCHIVE_DIR", ROOT / "archive"))
STATE_FILE = ARCHIVE / "state.json"

DEFAULTS = {
  "INFLUX_URL": "http://localhost:8086",
  "INFLUX_ORG": "comma",
  "INFLUX_BUCKET": "comma",
  "GRAFANA_URL": "http://localhost:3000",
  "GRAFANA_USER": "admin",
  "TZ_NAME": "UTC",
  "CLOUD_INTERVAL": "3600",
  "DEVICE_INTERVAL": "60",
  "DRIVER_NAME": "driver",
}


def _load_env() -> dict[str, str]:
  env = dict(DEFAULTS)
  f = ROOT / ".env"
  if f.exists():
    for line in f.read_text().splitlines():
      line = line.strip()
      if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
  env.update({k: v for k, v in os.environ.items() if k.isupper() and v != ""})
  return env


ENV = _load_env()

# secret name -> env var
SECRETS = {"access_token": "COMMA_TOKEN", "influx_token": "INFLUX_TOKEN", "ha_token": "HA_TOKEN",
           "grafana_token": "GRAFANA_TOKEN", "grafana_password": "GRAFANA_PASSWORD"}
# An older layout kept the outside accounts' tokens in a JSON file (AUTH_FILE). Only those are read from it: the
# InfluxDB/Grafana ones in such a file belong to some other install and would point this one at the wrong database.
AUTH_FILE_SECRETS = {"access_token", "ha_token"}


def secret(name: str, required: bool = True) -> str:
  val = ENV.get(SECRETS.get(name, name.upper()), "")
  if not val and name in AUTH_FILE_SECRETS and ENV.get("AUTH_FILE") and Path(ENV["AUTH_FILE"]).exists():
    val = json.loads(Path(ENV["AUTH_FILE"]).read_text()).get(name, "")
  if not val and required:
    raise SystemExit(f"missing {SECRETS.get(name, name.upper())} (run ./setup.py, or set it in .env)")
  return val


def ha_enabled() -> bool:
  """Driver names from Home Assistant (optional): a sensor whose state is the active driver's name."""
  return bool(ENV.get("HA_URL") and ENV.get("HA_DRIVER_ENTITY") and secret("ha_token", required=False))


def load_state() -> dict:
  if STATE_FILE.exists():
    return json.loads(STATE_FILE.read_text())
  return {"loaded": {}}


def save_state(state: dict) -> None:
  tmp = STATE_FILE.with_suffix(".tmp")
  tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
  tmp.replace(STATE_FILE)
