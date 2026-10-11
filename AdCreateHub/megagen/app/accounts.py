"""Google Cloud accounts: credentials, per-account clients, and the pool that
spreads generation jobs across them.

One gcloud sign-in can serve several projects. ADC sends
`x-goog-user-project: <quota_project_id>`; if that names project A while the
request path names project B you get the "quota project" error. So each account
gets its own credential copy whose quota project matches its own project.
"""
import threading
import time

from .config import load_config, notice, update_config

CLOUD_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# transient: the account is fine, it is just busy right now
TRANSIENT_MARKERS = ("429", "500", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE")
# hard: this account is misconfigured — using it again only wastes jobs
HARD_MARKERS = ("403", "PERMISSION_DENIED", "SERVICE_DISABLED",
                "has not been used", "is disabled", "credential", "Credential")

NO_ACCOUNT_MSG = ("No Project ID set — open ⚙ Settings, or click Guide if it's "
                  "your first time.")

# One lock for every mutable module-level structure below. It is never held
# across a sleep or a network call, so it cannot deadlock. RLock because
# health_of() is called from inside sections that already hold it.
_state_lock = threading.RLock()
_clients = {}        # acct_id -> (signature, client)
_health = {}         # acct_id -> health dict
_adc = None
_adc_failed = False


# ---------------- account list ----------------

def _normalise(raw, index):
    acct = {
        "id": str(raw.get("id") or f"acc{index + 1}"),
        "label": (str(raw.get("label") or "").strip() or f"Account {index + 1}")[:40],
        "project": str(raw.get("project") or "").strip(),
        "location": (str(raw.get("location") or "").strip() or "global"),
        "auth": "adc",                       # key files are a future addition
        "enabled": bool(raw.get("enabled", True)),
    }
    try:
        acct["credit"] = max(0.0, float(raw.get("credit", 0)))
    except (TypeError, ValueError):
        acct["credit"] = 0.0
    return acct


def list_accounts():
    """Every configured account. Migrates a legacy project/location pair the
    first time it is read — the migrated account keeps the $300 trial credit."""
    cfg = load_config()
    raw = cfg.get("accounts")
    if not isinstance(raw, list) or not raw:
        return [{"id": "acc1", "label": "Main",
                 "project": str(cfg.get("project") or "").strip(),
                 "location": (str(cfg.get("location") or "").strip() or "global"),
                 "auth": "adc", "credit": 300.0, "enabled": True}]
    return [_normalise(a, i) for i, a in enumerate(raw) if isinstance(a, dict)]


def save_accounts(accounts):
    """Persist, and mirror account 1 onto the legacy project/location keys so
    first-launch detection and any older build keep working."""
    first = accounts[0] if accounts else {"project": "", "location": "global"}
    update_config({"accounts": accounts,
                   "project": first.get("project", ""),
                   "location": first.get("location", "global")})


def enabled_accounts():
    """Accounts that could actually serve a job."""
    return [a for a in list_accounts() if a["enabled"] and a["project"]]


def find_account(acct_id):
    for a in list_accounts():
        if a["id"] == acct_id:
            return a
    return None


def next_account_id():
    used = {a["id"] for a in list_accounts()}
    i = 1
    while f"acc{i}" in used:
        i += 1
    return f"acc{i}"


# ---------------- health ----------------

def _blank_health(acct_id):
    return {"id": acct_id, "inflight": 0, "cooldown_until": 0.0, "consecutive": 0,
            "hard_error": "", "ok": 0, "failed": 0, "last_error": ""}


def health_of(acct_id):
    with _state_lock:
        h = _health.get(acct_id)
        if h is None:
            h = _health[acct_id] = _blank_health(acct_id)
        return h


def clear_health(acct_id):
    """A successful Test wipes a hard error so the account rejoins the pool."""
    with _state_lock:
        h = health_of(acct_id)
        h["hard_error"] = ""
        h["consecutive"] = 0
        h["cooldown_until"] = 0.0
        h["last_error"] = ""


def health_payload():
    """What the Settings panel renders. Called by GET /api/accounts only."""
    now = time.time()
    out = []
    for a in list_accounts():
        h = health_of(a["id"])
        cool = max(0, int(round(h["cooldown_until"] - now)))
        if not a["enabled"]:
            status, detail = "off", "disabled"
        elif not a["project"]:
            status, detail = "error", "no Project ID"
        elif h["hard_error"]:
            status, detail = "error", h["hard_error"][:120]
        elif cool > 0:
            status, detail = "cooling", f"cooling down {cool}s"
        elif h["inflight"] > 0:
            status, detail = "busy", f"{h['inflight']} in flight"
        else:
            status, detail = "ready", "ready"
        out.append({"id": a["id"], "label": a["label"], "project": a["project"],
                    "location": a["location"], "credit": a["credit"],
                    "enabled": a["enabled"], "status": status, "detail": detail,
                    "inflight": h["inflight"], "ok": h["ok"], "failed": h["failed"]})
    return out


# ---------------- credentials & clients ----------------

def _adc_credentials():
    """The machine's gcloud sign-in, loaded once per process."""
    global _adc, _adc_failed
    with _state_lock:
        if _adc is not None or _adc_failed:
            return _adc
        try:
            import google.auth
            _adc, _ = google.auth.default(scopes=CLOUD_SCOPES)
        except Exception:
            _adc, _adc_failed = None, True
        return _adc


def _credentials_for(acct):
    base = _adc_credentials()
    if base is None:
        raise RuntimeError("Google sign-in not found — do Step 4 of the Setup "
                           "guide (`gcloud auth application-default login`).")
    try:
        # the quota-project header must agree with the project in the URL path
        return base.with_quota_project(acct["project"])
    except Exception:
        return base          # older credential type: let the existing hint fire


def client_for(acct):
    """Cached client. Rebuilt when the account's project or location changes."""
    from .gen_engine import make_client        # local import: avoids a cycle
    sig = (acct["project"], acct["location"], acct["auth"])
    with _state_lock:
        hit = _clients.get(acct["id"])
        if hit and hit[0] == sig:
            return hit[1]
    client = make_client(acct["project"], acct["location"],
                         credentials=_credentials_for(acct))
    with _state_lock:
        _clients[acct["id"]] = (sig, client)
    return client


def forget_client(acct_id):
    """Drop the cached client and health after an edit or removal."""
    with _state_lock:
        _clients.pop(acct_id, None)
        _health.pop(acct_id, None)


def test_account(acct):
    """(ok, message) — the same hint branches as the single-account test."""
    from .gen_engine import GenError
    if not acct.get("project"):
        return False, "❌ No Project ID set for this account."
    try:
        client = client_for(acct)
        next(iter(client.models.list()))
        clear_health(acct["id"])
        return True, "✅ Connected! Your free-trial project is ready — go generate."
    except GenError as e:
        return False, f"❌ {e}"
    except Exception as e:
        msg = str(e).strip() or repr(e)
        hint = ""
        low = msg.lower()
        if "default credentials" in low or "credentials" in low or "401" in low:
            hint = "\n\n💡 Looks like Step 4 of the Setup guide hasn't run yet: `gcloud auth application-default login`"
        elif "quota project" in low:
            hint = "\n\n💡 Run: `gcloud auth application-default set-quota-project YOUR-PROJECT-ID`"
        elif "403" in low or "permission" in low:
            hint = "\n\n💡 Do Step 2 of the Setup guide (enable the API), wait 2 minutes, retry."
        elif "404" in low or "not found" in low:
            hint = "\n\n💡 Double-check the Project ID — it's the text ID, not the project number."
        return False, f"❌ {type(e).__name__}: {msg[:400]}{hint}"


# ---------------- the pool ----------------

class AccountPool:
    """Hands a client to each generation attempt, spreading work across every
    healthy account and stepping around one that is rate-limited.

    Snapshots the enabled accounts at construction, so editing Settings mid-run
    never changes a run that is already going."""

    WAIT_TICK = 0.25
    MAX_WAIT = 70.0          # longer than the worst cooldown (60s)

    def __init__(self, cap_per_account, cancel_check=None):
        self.cap = max(1, int(cap_per_account))
        self.cancel_check = cancel_check or (lambda: False)
        self.accounts = enabled_accounts()
        self._labels = {a["id"]: a["label"] for a in self.accounts}
        self._rr = 0

    def enabled_count(self):
        return len(self.accounts)

    def label_of(self, acct_id):
        return self._labels.get(acct_id, "")

    def capacity(self):
        """Total concurrent requests this run may have in flight."""
        return max(1, self.cap * max(1, len(self.accounts)))

    def _index(self, acct_id):
        for i, a in enumerate(self.accounts):
            if a["id"] == acct_id:
                return i
        return 0

    def _rank(self, a):
        h = health_of(a["id"])
        n = max(1, len(self.accounts))
        # fewest in flight, then fewest recent failures, then round-robin
        return (h["inflight"], h["consecutive"], (self._index(a["id"]) - self._rr) % n)

    def has_alternative(self, exclude_id):
        """True when some OTHER account could take this job right now — the
        caller uses it to skip the backoff sleep entirely."""
        now = time.time()
        with _state_lock:
            for a in self.accounts:
                if a["id"] == exclude_id:
                    continue
                h = health_of(a["id"])
                if not h["hard_error"] and h["cooldown_until"] <= now:
                    return True
        return False

    def cooldowns_clear(self):
        """True when no enabled account is sitting in a rate-limit cooldown —
        the §C7 second pass uses this to end its wait early."""
        now = time.time()
        with _state_lock:
            for a in self.accounts:
                h = health_of(a["id"])
                if not h["hard_error"] and h["cooldown_until"] > now:
                    return False
        return True

    def acquire(self):
        """Reserve a slot on the best account.

        Returns (acct_id, client, None) on success — the caller MUST pair every
        success with exactly one release().
        Returns (None, None, "cancelled") if the run was stopped while waiting.
        Returns (None, None, "<user-facing message>") when no account can serve
        this job at all — never blocks forever."""
        deadline = time.time() + self.MAX_WAIT
        while True:
            if self.cancel_check():
                return None, None, "cancelled"
            chosen = None
            with _state_lock:
                usable = [a for a in self.accounts
                          if not health_of(a["id"])["hard_error"]]
                if not usable:
                    return None, None, self._dead_message()
                now = time.time()
                ready = [a for a in usable
                         if health_of(a["id"])["cooldown_until"] <= now
                         and health_of(a["id"])["inflight"] < self.cap]
                if ready:
                    ready.sort(key=self._rank)
                    chosen = ready[0]
                    health_of(chosen["id"])["inflight"] += 1      # reserve
                    self._rr = (self._index(chosen["id"]) + 1) % max(1, len(self.accounts))
            if chosen is not None:
                try:
                    return chosen["id"], client_for(chosen), None
                except Exception as e:
                    with _state_lock:
                        h = health_of(chosen["id"])
                        h["inflight"] = max(0, h["inflight"] - 1)
                    self._mark_hard(chosen["id"], str(e))
                    continue                                     # try another
            if time.time() > deadline:
                return None, None, ("Every account is rate-limited right now — "
                                    "wait a minute and press Retry.")
            time.sleep(self.WAIT_TICK)          # never holding the lock

    def release(self, acct_id, ok, msg="", transient=False, hard=None):
        """Free the slot. `transient` puts the account in a short cooldown;
        `hard` (None = auto-detect from the message) drops it for this session."""
        if not acct_id:
            return
        became_hard = ""
        with _state_lock:
            h = health_of(acct_id)
            h["inflight"] = max(0, h["inflight"] - 1)
            if ok:
                h["ok"] += 1
                h["consecutive"] = 0
                h["cooldown_until"] = 0.0
                return
            h["failed"] += 1
            h["last_error"] = msg[:200]
            if transient:
                h["consecutive"] += 1
                h["cooldown_until"] = time.time() + min(2 ** h["consecutive"], 60)
            else:
                is_hard = (any(m in msg for m in HARD_MARKERS)
                           if hard is None else bool(hard))
                if is_hard and not h["hard_error"]:
                    h["hard_error"] = msg[:200]
                    became_hard = h["hard_error"]
        if became_hard:                          # notice() outside the lock
            notice(f"Account “{self.label_of(acct_id)}” was switched off for this "
                   f"session: {became_hard[:140]}")

    def _mark_hard(self, acct_id, msg):
        with _state_lock:
            health_of(acct_id)["hard_error"] = msg[:200]
        notice(f"Account “{self.label_of(acct_id)}” couldn't be used: {msg[:140]}")

    def _dead_message(self):
        if not self.accounts:
            return NO_ACCOUNT_MSG
        broken = [f"{self.label_of(a['id'])}: {health_of(a['id'])['hard_error'][:80]}"
                  for a in self.accounts if health_of(a["id"])["hard_error"]]
        return "No usable Google Cloud account — " + "; ".join(broken)
