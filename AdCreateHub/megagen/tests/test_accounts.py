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
      A.health_of("acc1")["cooldown_until"] > _real_time.time())
check("no backoff sleep while a sibling was healthy", SLEPT == [], SLEPT)

# 3. single account DOES still back off (regression)
set_accounts([BASE[0]]); CALLS.clear(); SLEPT.clear()
pool, res = run_jobs(1)
check("single rate-limited account still sleeps", len(SLEPT) == 4, SLEPT)
check("and finally reports the error", res[0][1] and "429" in res[0][1], res[0][1])

# 4. hard error drops the account after ONE attempt. A non-retryable error
# returns immediately (generate_one never re-acquires within the same job —
# that's the existing, untouched retry contract), so the job that DISCOVERS
# the bad account is itself sacrificed; only jobs submitted after it benefit.
set_accounts(BASE); CALLS.clear(); SLEPT.clear()
FAIL.clear(); FAIL["acc1"] = "403 PERMISSION_DENIED on project p-one"
pool, res = run_jobs(3)
check("hard-failed account tried at most once", CALLS.count("acc1") == 1, CALLS)
check("hard error recorded", bool(A.health_of("acc1")["hard_error"]))
check("jobs after the discovery landed on the survivor", all(r[0] for r in res[1:]), res[1:])

# 5. no usable account -> a real message, never a hang. Needs a 3rd job: job 1
# discovers acc1 is hard, job 2 discovers acc2 is hard (each via its own failed
# attempt) — only a job submitted AFTER both are marked hard ever reaches
# acquire()'s "usable == []" branch and gets the dead-pool message.
set_accounts(BASE); FAIL.clear()
FAIL["acc1"] = FAIL["acc2"] = "403 PERMISSION_DENIED"
pool, res = run_jobs(3)
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
