"""config.json load/save + the paths every other module works from."""
import json
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "output"
TRASH_DIR = OUT_DIR / ".trash"
STATIC_DIR = BASE_DIR / "static"
CONFIG_FILE = BASE_DIR / "config.json"
USAGE_FILE = BASE_DIR / "usage.json"

DEFAULT_PROJECT = "Default"
FREE_TRIAL_CREDIT = 300.0

DEFAULTS = {
    "project": "",
    "location": "global",
    "accounts": [],          # [] means "migrate from project/location on first read"
    "last_project": DEFAULT_PROJECT,
    "last_tab": "images",
    "workers": 3,
    "img": {"model": "Nano Banana 2", "aspect": "auto", "size": "1K", "outputs": 1},
    "vid": {"model": "Veo 3.1 Fast · video", "aspect": "16:9", "res": "720p",
            "dur": 8, "audio": True, "outputs": 1},
    "grid_cols": 6,
    "hover_previews": True,
}

_cfg_lock = threading.Lock()

# One-shot warnings the UI should toast (unwritable config/usage, §10.11).
# Kept as a list so /api/state can hand them to the frontend, which dedupes.
NOTICES = []


def notice(msg):
    """Surface a background warning to the UI once — never spam it."""
    if msg not in NOTICES:
        NOTICES.append(msg)
        if len(NOTICES) > 8:
            del NOTICES[0]


def _merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _raw():
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            pass
    return {}


def load_config():
    """Saved config with every missing key filled in from DEFAULTS."""
    return _merge(DEFAULTS, _raw())


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=1, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        notice(f"Couldn't save settings to {CONFIG_FILE} — is the folder read-only?")
        return False


def update_config(partial):
    """Deep-merge a partial dict into config.json. Returns the full new config."""
    with _cfg_lock:
        cfg = _merge(_raw(), partial or {})
        save_config(cfg)
        return _merge(DEFAULTS, cfg)


def ensure_dirs():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
