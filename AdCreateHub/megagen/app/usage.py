"""usage.json spend tracker — ported from bulk_banana_gui.py."""
import json
import threading

from .config import USAGE_FILE, FREE_TRIAL_CREDIT, notice

SESSION = {"images": 0, "cost": 0.0}
_usage_lock = threading.Lock()


def load_usage():
    if USAGE_FILE.exists():
        try:
            u = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
            if isinstance(u, dict):
                accts = u.get("accounts")
                return {"images": int(u.get("images", 0)),
                        "est_cost": float(u.get("est_cost", 0.0)),
                        "accounts": accts if isinstance(accts, dict) else {}}
        except Exception:
            pass
    return {"images": 0, "est_cost": 0.0, "accounts": {}}


def save_usage(usage):
    try:
        USAGE_FILE.write_text(json.dumps(usage), encoding="utf-8")
    except Exception:
        notice(f"Couldn't save the spend tracker to {USAGE_FILE} — all-time totals "
               "won't survive a relaunch. Is the folder read-only or cloud-synced?")


def add_usage(images=1, cost=0.0, account_id=None):
    with _usage_lock:
        u = load_usage()
        u["images"] += images
        u["est_cost"] += cost
        if account_id:
            row = u["accounts"].setdefault(account_id, {"images": 0, "est_cost": 0.0})
            row["images"] += images
            row["est_cost"] += cost
        save_usage(u)
        SESSION["images"] += images
        SESSION["cost"] += cost


def reset_tracker():
    with _usage_lock:
        save_usage({"images": 0, "est_cost": 0.0, "accounts": {}})
        SESSION["images"] = 0
        SESSION["cost"] = 0.0


def usage_payload():
    from . import accounts as accounts_mod
    u = load_usage()
    accts = accounts_mod.list_accounts()
    credit = sum(a["credit"] for a in accts if a["enabled"])
    per = {}
    for a in accts:
        row = u["accounts"].get(a["id"], {"images": 0, "est_cost": 0.0})
        per[a["id"]] = {"label": a["label"], "images": int(row["images"]),
                        "est_cost": round(float(row["est_cost"]), 4),
                        "credit": a["credit"],
                        "left": round(max(a["credit"] - float(row["est_cost"]), 0.0), 4)}
    return {
        "images": u["images"],
        "est_cost": round(u["est_cost"], 4),
        "session_images": SESSION["images"],
        "session_cost": round(SESSION["cost"], 4),
        "credit": round(credit, 2),
        "left": round(max(credit - u["est_cost"], 0.0), 4),
        "per_account": per,
    }
