# MegaGen — Change Specification for Claude Code
## Multi-account generation: spread every run across two or more Google Cloud projects

---

## §0. OPERATING RULES FOR CLAUDE CODE — READ FIRST

1. **Every decision has been made.** This document tells you exactly what to build, where, and how. Your job is implementation. Do not redesign, rename, simplify, or add anything beyond this spec.
2. **Ask the user only if** two parts of this spec directly contradict each other, or a referenced symbol genuinely does not exist in the codebase. Everything else: build as written.
3. **Respect the app's philosophy.** Vanilla JS (no frameworks, no build step), FastAPI backend, everything re-renders from `/api/state` after `bump()`, styling only through existing CSS custom properties and classes. **Zero new dependencies** — `google-auth` already ships as a `google-genai` dependency. `requirements.txt` is unchanged.
4. **Reuse existing machinery.** The pool is deliberately built on the existing run registry, `_new_run`/`_launch`/`_run_worker`, `config.notice()`, and the existing toast/confirm helpers. Do not invent parallel systems.
5. **Do not touch** (regression contract, §10): `gen_image`, `gen_video_veo`, `gen_video_omni`, `_mp4_duration_seconds`, `_camel`, `_video_bytes_from_op`, the seq/name reservation semantics (`reserve_seqs`, `reserve_seq_after`, `claim_name`), media sorting `(n, seq, ts, name)`, the trash/restore flow, the preserved-inputs flow (`projects.input_path` / `preserve_input`), the grid virtualisation, the lightbox, the launcher, port 7861, or the pricing tables.
6. When done, verify every line of the §12 Definition of Done. Items marked 🔌 need a configured Google project; everything else must be verified without one.

**File layout reminder:** backend in `app/` (`gen_engine.py`, `server.py`, `config.py`, `usage.py`, `projects.py`, `prompts.py`, `thumbs.py`), frontend in `static/` (`app.js`, `grid.js`, `lightbox.js`, `settings.js`, `index.html`, `style.css`). `run_megagen.py` at the repo root is **not touched**.

---

## §1. Why this change exists

Every run today builds **one** `genai.Client` from a single `project` + `location` and shares it across every job (`gen_engine.client_from_config`, called in six places). Vertex AI meters generative-model quota **per project × region × model**, so one project is the ceiling. At bulk volume jobs return `429 / RESOURCE_EXHAUSTED`, and `generate_one` then sleeps `2s → 4s → 8s → 16s` against the *same* saturated project before giving up into `failed_jobs`.

Adding a second project gives a second, independent quota bucket. Two wins:

1. **Concurrency** rises from `workers` to `workers × accounts`.
2. **The backoff disappears** when a healthy sibling exists — a rate-limited job moves to the other project immediately instead of sleeping. This is the larger win on a big batch.

### What this does and does not fix

| | |
|---|---|
| ✅ `429` / `RESOURCE_EXHAUSTED` | Per-project quota. Two projects = two buckets. The real win. |
| ✅ Throughput | `workers × accounts` concurrent requests. |
| ⚠️ Veo video quota | Per-project and already very low; doubling a small number is still small. |
| ❌ `503 UNAVAILABLE` | Google-side capacity, not your quota. Existing backoff still handles it. |
| ❌ A second $300 | The free trial is per **billing account**. A second project on the same login bills to the same account — new quota, **no new credit**. |

Because of that last row, **new accounts default to `credit: 0`** so the header total stays honest. Only the migrated first account carries $300.

---

## §1b. Locked product decisions (do not re-litigate)

1. **ADC only.** Auth is the machine's existing `gcloud auth application-default login`. Each account gets a per-account credential via `with_quota_project()`. **No service-account key files, no key-file UI, no new sign-in.** The account record still carries `"auth": "adc"` so key support can be added later without a migration.
2. **Spread across all** enabled accounts by fewest-in-flight. Not failover-only.
3. **Per-account credit field**, summed for the header total.
4. **Accounts are the source of truth** once they exist. Legacy `project`/`location` in `config.json` are mirrored from `accounts[0]` on every write so first-launch detection and older builds keep working.
5. **Recipes record the account** that produced an item, informationally only. Replay (🔄/✨/✏️) **never pins to it** — it always uses the live pool, so a removed account can never strand an item.
6. **Health is polled separately.** In-flight counts change constantly and must not churn `DATA_VERSION`; see §7.3.

---

## §2. `app/config.py` — accounts in DEFAULTS

Add one key to `DEFAULTS` (leave `project` and `location` exactly where they are):

```python
DEFAULTS = {
    "project": "",
    "location": "global",
    "accounts": [],          # [] means "migrate from project/location on first read"
    "last_project": DEFAULT_PROJECT,
    ...
}
```

Nothing else in `config.py` changes. Migration lives in `accounts.list_accounts()` (§3) so there is exactly one place that knows the shape.

### Account record

```json
{"id": "acc1", "label": "Main", "project": "veo3-502100", "location": "global",
 "auth": "adc", "credit": 300, "enabled": true}
```

`id` is immutable once minted (`acc1`, `acc2`, …). Usage and health key off `id`, **never** off `label`.

---

## §3. NEW FILE — `app/accounts.py`

Write this file exactly.

```python
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
```

---

## §4. `app/gen_engine.py` — changes

Add near the other imports:

```python
from . import accounts as accounts_mod
```

### 4.1 `make_client` gains an optional credential

```python
def make_client(project, location, credentials=None):
    project = (project or "").strip()
    location = (location or "").strip() or "global"
    if not project:
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")
    try:
        genai, _ = _sdk()
    except Exception:
        raise GenError("The google-genai library isn't installed. Close the app, open a "
                       "command window and run:  pip install -r requirements.txt")
    try:
        kwargs = {"vertexai": True, "project": project, "location": location}
        if credentials is not None:
            kwargs["credentials"] = credentials
        return genai.Client(**kwargs)
    except Exception as e:
        if "credential" in str(e).lower():
            raise GenError("Google sign-in not found — do Step 4 of the Setup guide "
                           "(`gcloud auth application-default login`).")
        raise
```

`test_connection(project, location)` and `client_from_config()` stay exactly as they are (still used by the legacy `/api/test_connection` route).

### 4.2 `generate_one` — pool-aware retry loop

`MAX_RETRIES`, the retryable substring list, the backoff formula, and the `traceback.print_exc()` are all preserved. **Only which client each attempt uses changes.**

```python
def generate_one(pool, model, prompt, image_paths, out_path, image_config):
    """(path|None, error|None, account_id|None).
    path=None AND error=None means the run was cancelled while waiting."""
    for attempt in range(1, MAX_RETRIES + 1):
        acct, client, reason = pool.acquire()
        if reason == "cancelled":
            return None, None, None
        if reason:
            return None, reason, None
        try:
            out_path.write_bytes(gen_image(client, model, prompt, image_paths, image_config))
            pool.release(acct, ok=True)
            return str(out_path), None, acct
        except Exception as e:
            msg = str(e).strip() or repr(e)
            retryable = any(c in msg for c in ("429", "500", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE"))
            pool.release(acct, ok=False, msg=msg, transient=retryable)
            if retryable and attempt < MAX_RETRIES:
                # a healthy sibling means the next acquire() lands elsewhere —
                # only sleep when there is nowhere else to go
                if not pool.has_alternative(acct):
                    time.sleep(min(2 ** attempt + random.uniform(0, 2), 60))
            else:
                traceback.print_exc()   # full details in the terminal window
                return None, f"{type(e).__name__}: {msg}"[:600], acct
    return None, "Gave up after retries (rate limited?)", None
```

### 4.3 `generate_one_video` — same shape

```python
def generate_one_video(pool, model_label, prompt, image_paths, out_path, vcfg):
    """Same contract as generate_one: (path, error, account_id)."""
    m = VIDEO_MODELS[model_label]
    for attempt in range(1, MAX_RETRIES + 1):
        acct, client, reason = pool.acquire()
        if reason == "cancelled":
            return None, None, None
        if reason:
            return None, reason, None
        try:
            if m["kind"] == "omni":
                data = gen_video_omni(client, m["id"], prompt, image_paths, vcfg)
            else:
                data = gen_video_veo(client, m["id"], prompt,
                                     image_paths[0] if image_paths else None, vcfg)
            out_path.write_bytes(data)
            pool.release(acct, ok=True)
            return str(out_path), None, acct
        except Exception as e:
            msg = str(e).strip() or repr(e)
            if m["kind"] == "omni" and ("NOT_FOUND" in msg or "404" in msg
                                        or "not found" in msg.lower()):
                # Omni not being enabled says nothing about this account's other
                # models — hard=False keeps it in the pool
                pool.release(acct, ok=False, msg=msg, transient=False, hard=False)
                return None, ("Omni Flash isn't switched on for Vertex on your project "
                              "yet (it's brand-new and Google is still rolling it out). "
                              "Pick a Veo model instead for now."), acct
            retryable = any(c in msg for c in ("429", "500", "503",
                                               "RESOURCE_EXHAUSTED", "UNAVAILABLE"))
            pool.release(acct, ok=False, msg=msg, transient=retryable)
            if retryable and attempt < MAX_RETRIES:
                if not pool.has_alternative(acct):
                    time.sleep(min(2 ** attempt + random.uniform(0, 2), 60))
            else:
                traceback.print_exc()
                return None, f"{type(e).__name__}: {msg}"[:600], acct
    return None, "Gave up after retries (rate limited?)", None
```

### 4.4 `_do_job` — 4-tuple

```python
def _do_job(state, pool, kind, model_label, job, cfg):
    """(path|None, error|None, cancelled, account_id|None)."""
    if state["cancel"]:
        return None, None, True, None
    out = Path(job["out"])
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        if kind == "video":
            path, err, acct = generate_one_video(pool, model_label, job["prompt_final"],
                                                 job["refs"], out, cfg)
        else:
            path, err, acct = generate_one(pool, MODELS[model_label], job["prompt_final"],
                                           job["refs"], out, cfg)
        # generate_one* signals "cancelled while waiting" with no path and no error
        return path, err, (path is None and err is None), acct
    except Exception as e:                      # e.g. Windows path/permission trouble
        traceback.print_exc()
        return None, f"{type(e).__name__}: {str(e)[:300]}", False, None
```

### 4.5 `_finish_job` — attribute usage and stamp the account

Change the signature to `_finish_job(project, kind, job, path, price, acct)` and, in place of the existing two lines:

```python
    info = dict(job["meta"])
    info["ts"] = int(time.time())
    if acct:
        info["account"] = acct
    projects.record_meta(project, final.name, info)
    cost = price(final) if callable(price) else price
    add_usage(1, cost, acct)
```

### 4.6 `_run_worker` — pool instead of client

Signature becomes `_run_worker(run_id, pool, kind, model_label, jobs, cfg, project, price)` (the `workers` argument is gone — capacity comes from the pool). Three edits inside:

```python
    state = RUNS[run_id]
    multi = pool.enabled_count() > 1
    try:
        with ThreadPoolExecutor(max_workers=pool.capacity()) as ex:
            futures = {ex.submit(_do_job, state, pool, kind, model_label, j, cfg): j
                       for j in jobs}
```

```python
                    try:
                        path, e, cancelled, acct = fut.result()
                    except Exception as exc:
                        path, e, cancelled, acct = None, f"{type(exc).__name__}: {exc}", False, None
                    if path:
                        try:
                            _finish_job(project, kind, job, path, price, acct)
```

```python
                        else:
                            tag = f" [{pool.label_of(acct)}]" if (multi and acct) else ""
                            state["errors"].append(f"{job['n']} v{job['v']}{tag}: {e}")
```

Everything else in `_run_worker` — the `done`/`pending`/`ok` bookkeeping, `failed_jobs`, `_release_seq`, `_release_name`, the finished-run pruning — is unchanged.

### 4.7 `_launch`

```python
def _launch(run, pool, kind, model_label, jobs, cfg, price):
    threading.Thread(
        target=_run_worker,
        args=(run["id"], pool, kind, model_label, jobs, cfg, run["project"], price),
        daemon=True).start()
    bump()
```

### 4.8 The six run-start functions

Add this helper just above `start_images`:

```python
def _make_pool(run, kind):
    """One pool per run. Video LROs stay capped at 2 per account (the ported
    quota rule); images use the workers slider per account."""
    workers = int(load_config().get("workers", 3))
    cap = min(workers, 2) if kind == "video" else workers
    return accounts_mod.AccountPool(cap, cancel_check=lambda: run["cancel"])
```

Then in each of the six functions:

- **Delete** the `client = client_from_config()` line.
- **Add**, immediately after the pre-flight validation (or at the top for the replay functions), the guard:

```python
    if not accounts_mod.enabled_accounts():
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")
```

- **Replace** the launch with a pool built from the run:

| Function | Old tail | New tail |
|---|---|---|
| `start_images` | `_launch(run, client, "image", s["model"], jobs, image_config, load_config().get("workers", 3), PRICE_PER_IMAGE...)` | `pool = _make_pool(run, "image")`<br>`_launch(run, pool, "image", s["model"], jobs, image_config, PRICE_PER_IMAGE.get(s["size"], 0.067))` |
| `start_videos` | `_launch(run, client, "video", s["model"], jobs, vcfg, min(...,2), price)` | `pool = _make_pool(run, "video")`<br>`_launch(run, pool, "video", s["model"], jobs, vcfg, price)` |
| `new_from` | `_launch(run, client, kind, model_label, [job], cfg, 1, price)` | `pool = _make_pool(run, kind)`<br>`_launch(run, pool, kind, model_label, [job], cfg, price)` |
| `edit_from` | `_launch(run, client, kind, model_label, jobs, cfg, workers, price)` | `pool = _make_pool(run, kind)`<br>`_launch(run, pool, kind, model_label, jobs, cfg, price)` — **delete** the local `workers = …` line |
| `retry_failed` | `_launch(run, client, kind, model_label, jobs, cfg, workers, price)` | `pool = _make_pool(run, kind)`<br>`_launch(run, pool, kind, model_label, jobs, cfg, price)` — **delete** both `workers = …` assignments |

- **`regenerate`** builds its own thread rather than calling `_launch`. Its tail becomes:

```python
    run = _new_run(kind, project, f"Regenerate {name}", [job], bulk=False, replaces=name)
    pool = _make_pool(run, kind)

    def _watch():
        _run_worker(run["id"], pool, kind, model_label, [job], cfg, project, price)
        with _plan_lock:
            _regen_inflight.discard((project, name))

    threading.Thread(target=_watch, daemon=True).start()
    bump()
    return {"run_id": run["id"], "total": 1}
```

Note `regenerate` currently calls `client_from_config()` inside its `try:` block — remove that line but keep the `try/except` that discards `_regen_inflight` on failure, since `_replay_job` can still raise.

### 4.9 File size

`gen_engine.py` will land around ~960 lines. If it exceeds ~950, move the four pure helpers `validate_images`, `validate_videos`, `_image_settings`, `_video_settings` verbatim into a new `app/planning.py` and import them back — they hold no shared state, so this is a zero-risk cut.

---

## §5. `app/usage.py` — per-account totals

```python
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
```

`FREE_TRIAL_CREDIT` stays imported and unused-by-default; leave the import in place.

---

## §6. `app/server.py` — five routes

Add `from . import accounts as accounts_mod` to the imports, then place these next to the other settings routes.

```python
@app.get("/api/accounts")
def api_accounts():
    return {"accounts": accounts_mod.health_payload(),
            "usage": usage.usage_payload()}


@app.post("/api/accounts")
async def api_account_add(request: Request):
    data = await body(request)
    accts = accounts_mod.list_accounts()
    acct = {"id": accounts_mod.next_account_id(),
            "label": (str(data.get("label") or "").strip() or f"Account {len(accts) + 1}")[:40],
            "project": str(data.get("project") or "").strip(),
            "location": (str(data.get("location") or "").strip() or "global"),
            "auth": "adc", "credit": 0.0, "enabled": True}
    accts.append(acct)
    accounts_mod.save_accounts(accts)
    gen_engine.bump()
    return {"ok": True, "id": acct["id"], "accounts": accounts_mod.health_payload()}


@app.patch("/api/accounts/{acct_id}")
async def api_account_patch(acct_id: str, request: Request):
    data = await body(request)
    accts = accounts_mod.list_accounts()
    hit = next((a for a in accts if a["id"] == acct_id), None)
    if not hit:
        return fail("That account is gone already.")
    if "label" in data:
        hit["label"] = (str(data["label"]).strip() or hit["label"])[:40]
    if "project" in data:
        hit["project"] = str(data["project"]).strip()
    if "location" in data:
        hit["location"] = str(data["location"]).strip() or "global"
    if "enabled" in data:
        hit["enabled"] = bool(data["enabled"])
    if "credit" in data:
        try:
            hit["credit"] = max(0.0, float(data["credit"]))
        except (TypeError, ValueError):
            pass
    accounts_mod.save_accounts(accts)
    accounts_mod.forget_client(acct_id)      # project/location may have changed
    gen_engine.bump()
    return {"ok": True, "accounts": accounts_mod.health_payload()}


@app.delete("/api/accounts/{acct_id}")
def api_account_delete(acct_id: str):
    accts = accounts_mod.list_accounts()
    if len(accts) <= 1:
        return fail("Keep at least one account.")
    left = [a for a in accts if a["id"] != acct_id]
    if len(left) == len(accts):
        return fail("That account is gone already.")
    accounts_mod.save_accounts(left)
    accounts_mod.forget_client(acct_id)
    gen_engine.bump()
    return {"ok": True, "accounts": accounts_mod.health_payload()}


@app.post("/api/accounts/{acct_id}/test")
def api_account_test(acct_id: str):
    acct = accounts_mod.find_account(acct_id)
    if not acct:
        return fail("That account is gone already.")
    ok, message = accounts_mod.test_account(acct)
    gen_engine.bump()
    return {"ok": ok, "message": message}
```

`/api/state` is **unchanged** except that its `usage` block now carries the extra fields from §5 automatically. `POST /api/test_connection` stays exactly as-is (no longer called by the UI, kept so nothing external breaks).

---

## §7. Frontend

### 7.1 `static/index.html` — replace the connection block

Replace lines ~196–205 (from `<h4>☁ Google Cloud connection</h4>` through `<div id="conn-status" class="status-panel"></div>`) with:

```html
      <h4>☁ Google Cloud accounts</h4>
      <p class="hint">Each account is one Google Cloud project. MegaGen spreads every
        run across all of them, so a project that hits its per-minute limit doesn't
        stall the batch — the work moves to the next one.</p>
      <div id="accounts-list"></div>
      <div class="row-btns">
        <button class="btn sm" id="btn-add-account">＋ Add account</button>
      </div>
```

Add the capacity line directly **after** the existing workers `<p class="hint">…3 is a safe default.</p>` in the Performance section:

```html
      <p class="hint mono" id="capacity-line"></p>
```

### 7.2 `static/style.css` — append

```css
/* ---------- account cards ---------- */
.acct-card {
  border: 1px solid var(--chrome);
  border-radius: 12px;
  padding: 10px 12px;
  margin-bottom: 8px;
  background: rgba(255, 255, 255, .015);
}
.acct-top { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.acct-top input.acct-label { flex: 1; min-width: 0; font-weight: 600; }
.acct-grid { display: flex; gap: 8px; flex-wrap: wrap; }
.acct-grid label { display: flex; flex-direction: column; gap: 4px; font-size: .72rem; color: var(--dim); }
.acct-grid label.wide { flex: 1; min-width: 180px; }
.acct-grid input { width: 100%; }
.acct-grid label.narrow { width: 120px; }
.acct-foot { display: flex; align-items: center; gap: 8px; margin-top: 9px; flex-wrap: wrap; }
.acct-status { font-size: .74rem; color: var(--dim); flex: 1; min-width: 160px; white-space: pre-wrap; }
.acct-health { display: inline-flex; align-items: center; gap: 5px; font-size: .72rem; }
.acct-health i { width: 8px; height: 8px; border-radius: 50%; background: var(--dim); display: inline-block; }
.acct-health.ready i { background: var(--ok); }
.acct-health.busy i { background: var(--accent-hi); animation: chip-pulse 1.1s ease-in-out infinite; }
.acct-health.cooling i { background: var(--warn); }
.acct-health.error i { background: var(--danger); }
.acct-health.off i { background: rgba(255, 255, 255, .2); }
.acct-dupe { color: var(--warn); font-size: .72rem; }
```

### 7.3 `static/settings.js`

**Delete** the `$('btn-save-cloud')` and `$('btn-test-conn')` handlers entirely (those elements no longer exist).

**In `Settings.sync`**, delete the two lines referencing `set-project` / `set-location`, and append the capacity line:

```js
  Settings.sync = function () {
    const $ = U.$, cfg = window.MG.cfg;
    $('set-workers').value = cfg.workers;
    $('set-workers-val').textContent = cfg.workers;
    $('set-hover-previews').checked = !!cfg.hover_previews;
    $('hover-previews-view').checked = !!cfg.hover_previews;
    $('grid-cols').value = cfg.grid_cols;
    $('grid-cols-val').textContent = cfg.grid_cols;
    Settings.renderCapacity();
  };

  Settings.renderCapacity = function () {
    const cfg = window.MG.cfg;
    const n = (accts || []).filter((a) => a.enabled && a.project).length || 1;
    const per = Number(cfg.workers) || 3;
    U.$('capacity-line').textContent = n > 1
      ? per + ' at a time per account · ' + n + ' accounts → up to ' + (per * n) + ' in parallel'
      : per + ' at a time';
  };
```

**Add** the account list, its poll, and its handlers:

```js
  let accts = [];
  let healthTimer = null;

  Settings.renderAccounts = function (rows, usage) {
    accts = rows || [];
    const $ = U.$, esc = U.esc;
    const seen = {};
    $('accounts-list').innerHTML = accts.map((a) => {
      const dupe = seen[a.project] && a.project;
      seen[a.project] = true;
      const spend = (usage && usage.per_account && usage.per_account[a.id]) || null;
      return '<div class="acct-card" data-id="' + esc(a.id) + '">'
        + '<div class="acct-top">'
        +   '<span class="acct-health ' + esc(a.status) + '"><i></i>' + esc(a.detail) + '</span>'
        +   '<input class="acct-label" data-f="label" value="' + esc(a.label) + '" maxlength="40">'
        +   '<label class="check-row" style="margin:0"><input type="checkbox" data-f="enabled"'
        +     (a.enabled ? ' checked' : '') + '> on</label>'
        + '</div>'
        + '<div class="acct-grid">'
        +   '<label class="wide">Project ID<input data-f="project" value="' + esc(a.project) + '" placeholder="my-project-448212" spellcheck="false"></label>'
        +   '<label class="narrow">Location<input data-f="location" value="' + esc(a.location) + '" spellcheck="false"></label>'
        +   '<label class="narrow">Credit $<input data-f="credit" type="number" min="0" step="1" value="' + a.credit + '"></label>'
        + '</div>'
        + (dupe ? '<div class="acct-dupe">Same project as an account above — no extra quota.</div>' : '')
        + '<div class="acct-foot">'
        +   '<button class="btn sm" data-act="test">Test</button>'
        +   '<button class="btn sm ghost" data-act="remove"' + (accts.length <= 1 ? ' disabled' : '') + '>Remove</button>'
        +   '<span class="acct-status">' + (spend ? '$' + spend.est_cost.toFixed(2) + ' spent · ' + spend.images + ' items' : '') + '</span>'
        + '</div>'
        + '</div>';
    }).join('');
    Settings.renderCapacity();
  };

  Settings.loadAccounts = async function () {
    try {
      const r = await U.api('/api/accounts');
      Settings.renderAccounts(r.accounts, r.usage);
    } catch (e) { U.oops(e); }
  };

  // in-flight counts change constantly; they must never churn DATA_VERSION,
  // so the panel polls for its own health only while it is open
  Settings.startHealthPoll = function () {
    Settings.stopHealthPoll();
    healthTimer = setInterval(() => {
      if (document.getElementById('settings-modal').classList.contains('hidden')) {
        Settings.stopHealthPoll();
        return;
      }
      if (!document.activeElement || !document.activeElement.closest('#accounts-list')) {
        Settings.loadAccounts();          // never repaint under the user's cursor
      }
    }, 2000);
  };
  Settings.stopHealthPoll = function () {
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  };
```

Wire it inside `Settings.init` (the `btn-settings` handler gains two calls, plus the list handlers):

```js
    $('btn-settings').onclick = () => {
      Settings.sync();
      Settings.loadTrash();
      Settings.loadAccounts();
      Settings.startHealthPoll();
      $('settings-modal').classList.remove('hidden');
    };

    $('btn-add-account').onclick = async () => {
      try {
        await jpost('/api/accounts', { label: 'Account ' + (accts.length + 1) });
        await Settings.loadAccounts();
        toast('Account added — paste its Project ID, then press Test.');
      } catch (e) { oops(e); }
    };

    $('accounts-list').addEventListener('change', async (e) => {
      const card = e.target.closest('.acct-card');
      const f = e.target.dataset.f;
      if (!card || !f) return;
      const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
      try {
        await U.api('/api/accounts/' + encodeURIComponent(card.dataset.id),
          { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [f]: value }) });
        await Settings.loadAccounts();
        await U.refresh();
      } catch (err) { oops(err); }
    });

    $('accounts-list').addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const card = e.target.closest('.acct-card');
      const id = card.dataset.id;
      if (btn.dataset.act === 'test') {
        card.querySelector('.acct-status').textContent = 'Testing…';
        try {
          const r = await jpost('/api/accounts/' + encodeURIComponent(id) + '/test', {});
          card.querySelector('.acct-status').textContent = r.message;
        } catch (err) { card.querySelector('.acct-status').textContent = '❌ ' + err.message; }
        return;
      }
      U.confirmBox('Remove this account? Media it already made is untouched.', async () => {
        try {
          await U.api('/api/accounts/' + encodeURIComponent(id), { method: 'DELETE' });
          await Settings.loadAccounts();
          await U.refresh();
        } catch (err) { oops(err); }
      }, 'Remove');
    });
```

Also add `Settings.stopHealthPoll()` to the workers `onchange` handler's end (after the `jpost`) — no; instead call `Settings.renderCapacity()` there so the line updates live:

```js
    $('set-workers').onchange = async (e) => {
      MG.cfg.workers = Number(e.target.value);
      Settings.renderCapacity();
      try { await jpost('/api/config', { workers: MG.cfg.workers }); } catch (err) { oops(err); }
    };
```

### 7.4 `static/app.js` — two edits

**(a)** Widen the per-prompt chip error regex in `promptStats` so the `[Label]` prefix doesn't break failure chips:

```js
        const m = /^(\d+) v\d+(?: \[[^\]]*\])?: (.*)$/.exec(line);
```

**(b)** The overlay-close handler must stop the health poll. In `wireStatic`, inside the `.overlay` click handler, after `ov.classList.add('hidden')`, add:

```js
          if (ov.id === 'settings-modal' && window.Settings) Settings.stopHealthPoll();
```

and the same call in the `Escape` branch of `wireKeys` where an overlay is closed.

Nothing else in `app.js` changes — the topbar and generate-bar already read `usage.credit` / `usage.left`, which now carry the summed figure automatically.

---

## §8. Guide text (`static/index.html`, guide modal)

After the `😵 If Test connection fails` section, insert:

```html
      <hr>
      <h3>➕ Adding a second project (more quota)</h3>
      <p>Google limits how many images or clips one project can make per minute. A second
      project is a second, separate limit — MegaGen spreads every run across all the
      accounts you add, so hitting a limit on one no longer stalls the batch.</p>
      <ul>
        <li>In <a href="https://console.cloud.google.com/projectcreate" target="_blank">the Cloud console</a>
          create a new project, then <a href="https://console.cloud.google.com/apis/library/aiplatform.googleapis.com" target="_blank">enable the API</a> on it (Step 2 again, for the new project).</li>
        <li>⚙ Settings → <b>＋ Add account</b> → paste the new Project ID → <b>Test</b>.</li>
        <li>No second sign-in is needed — your existing <code>gcloud</code> login already covers
          every project you own.</li>
        <li><b>It doubles your speed, not your money.</b> The $300 trial belongs to your
          billing account, not to a project, so a second project on the same account shares
          the same $300. That's why new accounts start with a credit of <b>$0</b> — set it
          only if that project really bills separately.</li>
      </ul>
```

---

## §9. Edge cases the implementation must handle

1. **Removing the last account** → 400 `Keep at least one account.`
2. **Every account hard-failed mid-run** → `acquire()` returns the `_dead_message()` immediately; jobs fail with a real message. It **never blocks forever**.
3. **All accounts merely cooling down** → `acquire()` waits, re-checking cancel every 0.25s, and gives up after 70s with `Every account is rate-limited right now — wait a minute and press Retry.`
4. **Cancel during a wait** → `acquire()` returns `"cancelled"` → `generate_one` returns `(None, None, None)` → `_do_job` reports `cancelled=True` → the job counts toward `done`, **not** into `failed_jobs` (existing rule preserved).
5. **Second project missing the API** → first job returns 403 → `HARD_MARKERS` matches → account drops out of the pool, one `notice()` toast, the run finishes on the other account.
6. **Omni Flash not enabled** → released with `hard=False`, so the account stays available for Veo and images.
7. **Safety-filter errors** (`No image in response…`) → neither transient nor hard: counted as a failure, account stays healthy.
8. **Editing an account mid-run** → the pool snapshots `enabled_accounts()` at construction, so a running batch is unaffected; the next run picks up the change.
9. **`with_quota_project` unsupported** → falls back to the base credential; the existing `"quota project"` hint still fires on Test.
10. **Duplicate project IDs** → allowed, flagged inline (`Same project as an account above — no extra quota.`).
11. **Account removed while its items exist** → recipes keep a stale `account` string; replay ignores it entirely.
12. **Every `acquire()` success is paired with exactly one `release()`** — including both early-return paths in `generate_one_video`.
13. **Lock discipline** → `_state_lock` is the only lock in `accounts.py`; it is never held across a `sleep`, a network call, or a `notice()`.

---

## §10. Regression contract — must still be true afterwards

- With **exactly one account**, behaviour is byte-identical to today: same concurrency (`workers`, and `min(workers, 2)` for video), same error strings with **no** `[Label]` prefix, same backoff (`has_alternative` is false, so the sleep still happens).
- 🔄 Regenerate keeps the filename and grid slot; ✨ New lands at `seq + 0.001`; ✏️ Edit lands after its source; `Retry N failed` reuses planned filenames and seqs.
- Cleared references still replay from `.inputs/` (`projects.input_path`).
- Cancelled jobs never enter `failed_jobs`.
- Ghost tiles, the virtualised grid, lasso/shortcuts, lightbox zoom, hover previews, trash restore, `.txt` drop and Clear-all are untouched.
- First launch (`config.json` absent) still auto-opens Settings.
- No renamed ids/classes outside the replaced connection block; no new dependencies; `requirements.txt` unchanged; port 7861 unchanged.

---

## §11. Tests — `tests/test_accounts.py`

Run with `python tests/test_accounts.py` from the repo root. It must **not** touch the real API or the real `usage.json`.

```python
"""Multi-account pool with stubbed clients — no credits spent.

Isolation matters here: this suite must never touch the real config.json or
usage.json, and must never patch the global time module (accounts.py uses
time.sleep for its own wait loop)."""
import shutil, sys, tempfile, time as _real_time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import accounts as A, config, gen_engine, usage

# --- isolation: redirect config + usage at a scratch dir BEFORE anything reads them
_TMP = Path(tempfile.mkdtemp(prefix="megagen-pooltest-"))
config.CONFIG_FILE = _TMP / "config.json"
config.USAGE_FILE = _TMP / "usage.json"
usage.USAGE_FILE = config.USAGE_FILE          # usage.py bound it at import time

CALLS = []                      # account ids, in the order they were used
FAIL = {}                       # account_id -> exception message to raise
SLEPT = []

ok, bad = [], []
def check(label, cond, detail=""):
    (ok if cond else bad).append(label)
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  -> " + str(detail)) if detail else ""))

# --- stub the SDK boundary, keep the real pool/retry logic ---
A.client_for = lambda acct: ("client", acct["id"])

class _Clock:
    """Records gen_engine's backoff sleeps without touching the real time module
    that accounts.AccountPool.acquire() relies on."""
    def __getattr__(self, k):
        return getattr(_real_time, k)
    def sleep(self, s):
        SLEPT.append(s)
gen_engine.time = _Clock()                    # rebinds ONLY gen_engine's name

def fake_gen_image(client, model, prompt, image_paths, image_config):
    acct_id = client[1]
    CALLS.append(acct_id)
    if acct_id in FAIL:
        raise RuntimeError(FAIL[acct_id])
    return b"\x89PNG\r\n\x1a\n"                        # bytes are never opened
gen_engine.gen_image = fake_gen_image

def set_accounts(rows):
    A.save_accounts(rows)
    A._clients.clear(); A._health.clear()

BASE = [{"id": "acc1", "label": "One", "project": "p-one", "location": "global",
         "auth": "adc", "credit": 300.0, "enabled": True},
        {"id": "acc2", "label": "Two", "project": "p-two", "location": "global",
         "auth": "adc", "credit": 0.0, "enabled": True}]

def run_jobs(count, cap=3):
    pool = A.AccountPool(cap)
    out = []
    for i in range(count):
        out.append(gen_engine.generate_one(pool, "m", "p", [], _TMP / f"_t{i}.png", {}))
    return pool, out

# 1. spread
set_accounts(BASE); CALLS.clear(); FAIL.clear()
pool, res = run_jobs(6)
check("capacity is cap x accounts", pool.capacity() == 6, pool.capacity())
check("all 6 succeeded", all(r[0] for r in res), res)
check("work spread across both accounts",
      CALLS.count("acc1") == 3 and CALLS.count("acc2") == 3, CALLS)

# 2. transient failover, and NO backoff sleep
set_accounts(BASE); CALLS.clear(); SLEPT.clear()
FAIL.clear(); FAIL["acc1"] = "429 RESOURCE_EXHAUSTED"
pool, res = run_jobs(4)
check("every job still completed", all(r[0] for r in res), [r[1] for r in res])
check("all output came from the healthy account",
      all(r[2] == "acc2" for r in res), [r[2] for r in res])
check("rate-limited account was put in cooldown",
      A.health_of("acc1")["cooldown_until"] > time.time())
check("no backoff sleep while a sibling was healthy", SLEPT == [], SLEPT)

# 3. single account DOES still back off (regression)
set_accounts([BASE[0]]); CALLS.clear(); SLEPT.clear()
pool, res = run_jobs(1)
check("single rate-limited account still sleeps", len(SLEPT) == 4, SLEPT)
check("and finally reports the error", res[0][1] and "429" in res[0][1], res[0][1])

# 4. hard error drops the account after ONE attempt
set_accounts(BASE); CALLS.clear(); SLEPT.clear()
FAIL.clear(); FAIL["acc1"] = "403 PERMISSION_DENIED on project p-one"
pool, res = run_jobs(3)
check("hard-failed account tried at most once", CALLS.count("acc1") == 1, CALLS)
check("hard error recorded", bool(A.health_of("acc1")["hard_error"]))
check("run completed on the survivor", all(r[0] for r in res))

# 5. no usable account -> a real message, never a hang
set_accounts(BASE); FAIL.clear()
FAIL["acc1"] = FAIL["acc2"] = "403 PERMISSION_DENIED"
pool, res = run_jobs(2)
check("dead pool returns a message, not None",
      res[-1][1] and "No usable Google Cloud account" in res[-1][1], res[-1][1])

# 6. cancel returns promptly and reads as cancelled
set_accounts(BASE); FAIL.clear()
pool = A.AccountPool(3, cancel_check=lambda: True)
acct, client, reason = pool.acquire()
check("cancel is signalled distinctly", reason == "cancelled", reason)
t0 = _real_time.time()
r = gen_engine.generate_one(pool, "m", "p", [], _TMP / "_c.png", {})
check("cancelled job returns (None, None, None)", r == (None, None, None), r)
check("cancel is fast", _real_time.time() - t0 < 1.0)

# 7. per-account usage
set_accounts(BASE)
u0 = usage.load_usage()
usage.add_usage(1, 0.10, "acc2")
u1 = usage.load_usage()
check("usage attributed to the account",
      u1["accounts"]["acc2"]["images"] == u0["accounts"].get("acc2", {}).get("images", 0) + 1)
check("totals still move", round(u1["est_cost"] - u0["est_cost"], 3) == 0.10)
p = usage.usage_payload()
check("credit is the sum of enabled accounts", p["credit"] == 300.0, p["credit"])
check("per-account breakdown present", "acc2" in p["per_account"])

shutil.rmtree(_TMP, ignore_errors=True)
print()
print(f"{len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
```

`shutil` is imported at the top alongside `sys`. Because `config.CONFIG_FILE` / `USAGE_FILE` are redirected before the first read, this suite cannot touch the user's real settings or spend tracker.

**Also update the existing scratch suites** (`test_engine.py`, `test_video.py`, `test_edit.py`) if they are present: they monkeypatch `gen_engine.generate_one` with the **old** signature. Change each stub to

```python
def stub(pool, model, prompt, image_paths, out_path, image_config):
    ...
    return str(out_path), None, "acc1"          # 3-tuple now
```

and the video stub to `def stub_video(pool, model_label, prompt, image_paths, out_path, vcfg)` returning a 3-tuple. Their assertions are otherwise unchanged and **all must still pass**.

---

## §12. DEFINITION OF DONE — verify each line (🔌 = needs a configured Google project)

- [ ] `python run_megagen.py` starts clean; no browser console errors; Settings opens.
- [ ] A `config.json` containing only `project`/`location` migrates silently to one account labelled **Main** with credit 300; the file afterwards contains both `accounts` **and** the mirrored `project`/`location`.
- [ ] A fresh (absent) `config.json` still auto-opens Settings on first launch.
- [ ] `＋ Add account` creates a second card with credit **0**; editing label/project/location/credit/enabled persists across a reload; **Remove** is disabled when only one account remains and 400s if forced.
- [ ] Two accounts with the same Project ID show `Same project as an account above — no extra quota.`
- [ ] `python tests/test_accounts.py` → all checks pass.
- [ ] The three existing stub suites pass unmodified apart from the new stub signature.
- [ ] With **one** account: `capacity() == workers`, error strings carry **no** `[Label]` prefix, and a rate-limited job still sleeps (regression).
- [ ] With **two** accounts: the Performance section reads `3 at a time per account · 2 accounts → up to 6 in parallel`.
- [ ] Health chips update about every 2s while Settings is open and stop polling when it closes; typing in a field is never interrupted by a repaint.
- [ ] 🔌 **Test** on a good account shows the green message; on a bogus Project ID shows the 403/404 hint; a passing Test clears a previous hard error.
- [ ] 🔌 A 10-prompt × 2-output image run with two real projects: both health chips show in-flight counts, both `per_account` spend figures rise, and the run card shows no `429` entries.
- [ ] 🔌 Disable account 2 mid-session, start a new run → everything goes to account 1 and error strings lose the `[Label]` prefix.
- [ ] **Stop** halts a run promptly even while an account is cooling down.
- [ ] 🔄 / ✨ / ✏️ / `Retry N failed` all still work, including after **Clear all references** (preserved-inputs path untouched).
- [ ] `git diff` touches only: `app/accounts.py` (new), `app/config.py`, `app/gen_engine.py`, `app/usage.py`, `app/server.py`, `static/index.html`, `static/settings.js`, `static/app.js`, `static/style.css`, `tests/test_accounts.py` (new), and optionally `app/planning.py`.

**When every box checks, stop. Do not add anything beyond this document.**
