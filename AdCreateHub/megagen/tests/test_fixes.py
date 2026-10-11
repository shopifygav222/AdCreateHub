"""v1.1 fixes: friendly errors (§C2), bounded MP4 read (F-C), trash lock
handling (§C3), the meta-store cache (F-D), and the automatic second pass for
rate-limited jobs (§C7).

Stubbed at the generate_one boundary — no real accounts, no credits spent.
Isolated from the user's real config.json / usage.json / output/.trash."""
import shutil, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import accounts as A, config, gen_engine, projects, prompts as pm, thumbs, usage
from app.errors import humanize

_TMP = Path(tempfile.mkdtemp(prefix="megagen-fixtest-"))
config.CONFIG_FILE = _TMP / "config.json"
config.USAGE_FILE = _TMP / "usage.json"
usage.USAGE_FILE = config.USAGE_FILE

# trash isolation: every module that bound TRASH_DIR at import time
_SCRATCH_TRASH = _TMP / ".trash"
config.TRASH_DIR = _SCRATCH_TRASH
projects.TRASH_DIR = _SCRATCH_TRASH
thumbs.TRASH_DIR = _SCRATCH_TRASH

A.save_accounts([{"id": "acc1", "label": "One", "project": "p-one", "location": "global",
                  "auth": "adc", "credit": 300.0, "enabled": True}])

ok, bad = [], []
def check(label, cond, detail=""):
    (ok if cond else bad).append(label)
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  -> " + str(detail)) if detail else ""))


# ---- 1. humanize (§C2): mapping, passthrough, fallback ----
check("429 maps to the friendly rate-limit line",
      "per-minute limit" in humanize("429 RESOURCE_EXHAUSTED: Quota exceeded for ..."))
check("403 maps to the enable-the-API line",
      "Step 2" in humanize("PermissionDenied: 403 aiplatform.googleapis.com is disabled"))
check("safety maps",
      "safety filter" in humanize("No image in response (safety filter?)"))
check("credentials map",
      "Step 4" in humanize("DefaultCredentialsError: could not find default credentials"))
check("pool message passes through untouched",
      humanize("Every account is rate-limited right now — wait a minute and press Retry.")
      .startswith("Every account"))
check("omni hint passes through untouched",
      humanize("Omni Flash isn't switched on for Vertex on your project yet.")
      .startswith("Omni Flash"))
check("unknown gets the trimmed fallback",
      humanize("XyzError: " + "very weird " * 40).startswith("Google said:"))
check("fallback is length-capped", len(humanize("XyzError: " + "x" * 500)) < 180)


# ---- 2. _mp4_duration_seconds (F-C): bounded head/tail read ----
def _mvhd_v0(ts, dur):
    # 'mvhd' + version0 + flags(3) + creation(4) + modification(4) + ts + dur
    return (b"mvhd" + bytes(12)
            + ts.to_bytes(4, "big") + dur.to_bytes(4, "big") + bytes(40))

small = _TMP / "small.mp4"
small.write_bytes(b"junkjunk" + _mvhd_v0(600, 4800))
check("head read parses duration", gen_engine._mp4_duration_seconds(small) == 8.0,
      gen_engine._mp4_duration_seconds(small))

big = _TMP / "big.mp4"
big.write_bytes(bytes(5_000_000) + _mvhd_v0(1000, 12345))
check("tail read finds a trailing moov",
      abs(gen_engine._mp4_duration_seconds(big) - 12.345) < 1e-6,
      gen_engine._mp4_duration_seconds(big))

junk = _TMP / "junk.bin"
junk.write_bytes(bytes(128))
check("garbage returns None", gen_engine._mp4_duration_seconds(junk) is None)

silly = _TMP / "silly.mp4"
silly.write_bytes(_mvhd_v0(1, 999_999))
check("insane duration is clamped to None", gen_engine._mp4_duration_seconds(silly) is None)


# ---- 3. _unlink_retry (§C3) ----
SLEEPS = []
class _Stubborn:
    def __init__(self, fails): self.fails = fails; self.calls = 0
    def unlink(self):
        self.calls += 1
        if self.calls <= self.fails:
            raise PermissionError("[WinError 32] The process cannot access the file")

s = _Stubborn(2)
check("retries through a transient Windows lock",
      projects._unlink_retry(s, attempts=6, delay=0.01, _sleep=lambda t: SLEEPS.append(t)) is True
      and s.calls == 3 and len(SLEEPS) == 2, (s.calls, SLEEPS))
s2 = _Stubborn(99)
check("gives up cleanly when the lock never clears",
      projects._unlink_retry(s2, attempts=4, delay=0.01, _sleep=lambda t: None) is False
      and s2.calls == 4, s2.calls)
missing = _TMP / "never-existed.png"
check("a missing file counts as deleted", projects._unlink_retry(missing) is True)


# ---- 4. empty_trash (§C3): locked files skipped and reported, never a crash ----
PROJ = "FixTest"
d = config.OUT_DIR / PROJ
if d.exists():
    shutil.rmtree(d)
projects.project_dir(PROJ)

img = projects.images_dir(PROJ) / "1-gav000001.png"
img.write_bytes(b"\x89PNG\r\n\x1a\n")
projects.record_meta(PROJ, img.name, {"kind": "image", "n": 1, "seq": 1.0, "prompt": "p"})
projects.trash_file(PROJ, img)

img2 = projects.images_dir(PROJ) / "2-gav000002.png"
img2.write_bytes(b"\x89PNG\r\n\x1a\n")
projects.record_meta(PROJ, img2.name, {"kind": "image", "n": 2, "seq": 1.0, "prompt": "p"})
locked_name = projects.trash_file(PROJ, img2)

_real_unlink_retry = projects._unlink_retry
projects._unlink_retry = lambda p, **kw: (p.name != locked_name) and _real_unlink_retry(p, **kw)
purged, locked = projects.empty_trash()
projects._unlink_retry = _real_unlink_retry
check("locked file is skipped, not fatal", purged == 1 and locked == [locked_name],
      (purged, locked))
check("locked file's recipe survives for a later attempt",
      locked_name in projects.load_trashmeta(), list(projects.load_trashmeta()))
purged2, locked2 = projects.empty_trash()
check("second attempt clears it", purged2 == 1 and locked2 == [], (purged2, locked2))
check("trash meta is empty afterwards", projects.load_trashmeta() == {})


# ---- 5. load_meta cache (F-D): copies out, invalidates on write and on disk change ----
projects.record_meta(PROJ, "x.png", {"kind": "image", "n": 9})
m1 = projects.load_meta(PROJ)
m1["INJECTED"] = True
check("callers get a copy, not the cache", "INJECTED" not in projects.load_meta(PROJ))
projects.record_meta(PROJ, "y.png", {"kind": "image", "n": 10})
check("record_meta invalidates the cache", "y.png" in projects.load_meta(PROJ))
mp = projects.meta_path(PROJ)
mp.write_text('{"z.png": {"kind": "image", "n": 11}}', encoding="utf-8")
check("an external rewrite is picked up",
      "z.png" in projects.load_meta(PROJ) and "y.png" not in projects.load_meta(PROJ))


# ---- 6. automatic second pass for transient failures (§C7) ----
class _Clock:
    """Forwards time() etc., swallows sleep() — cooloff + backoff become instant."""
    def __getattr__(self, k): return getattr(time, k)
    def sleep(self, s): pass
gen_engine.time = _Clock()

CALLS = {}
ALWAYS_FAIL = {"on": False}
def flaky_gen(pool, model, prompt, refs, out_path, cfg):
    """Odd-numbered calls per output fail with a transient 429; even succeed."""
    k = str(out_path)
    CALLS[k] = CALLS.get(k, 0) + 1
    if ALWAYS_FAIL["on"] or CALLS[k] % 2 == 1:
        return None, "429 RESOURCE_EXHAUSTED: rate limited", "acc1"
    Path(out_path).write_bytes(b"\x89PNG\r\n\x1a\n")
    return str(out_path), None, "acc1"
_real_generate_one = gen_engine.generate_one
gen_engine.generate_one = flaky_gen

def wait(run_id, limit=20):
    t0 = time.time()
    while time.time() - t0 < limit:
        r = gen_engine.RUNS.get(run_id)
        if r and r["finished"]:
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")

pm.save_prompts(PROJ, "images", [{"n": 1, "text": "one"}, {"n": 2, "text": "two"}])
CALLS.clear()
r = gen_engine.start_images(PROJ, {"model": "Nano Banana 2", "outputs": 2,
                                   "size": "1K", "aspect": "auto"})
run = wait(r["run_id"])
check("all outputs completed after the automatic second pass",
      run["ok"] == 4 and run["done"] == 4, (run["ok"], run["done"]))
check("no error surfaced for jobs the second pass rescued",
      run["errors"] == [], run["errors"])
check("each job was tried exactly twice", sorted(CALLS.values()) == [2, 2, 2, 2], CALLS)
check("nothing left pending", run["pending"] == {}, run["pending"])
check("no failed jobs recorded", run["failed_jobs"] == [], run["failed_jobs"])

# persistent failure: exactly one automatic extra try, then a friendly error
CALLS.clear()
ALWAYS_FAIL["on"] = True
r2 = gen_engine.start_images(PROJ, {"model": "Nano Banana 2", "outputs": 1,
                                    "size": "1K", "aspect": "auto"})
run2 = wait(r2["run_id"])
check("persistent 429 tries exactly twice per job",
      sorted(CALLS.values()) == [2, 2], CALLS)
check("persistent 429 lands in failed_jobs for the Retry button",
      len(run2["failed_jobs"]) == 2, len(run2["failed_jobs"]))
check("the surfaced error is the friendly one (§C2)",
      bool(run2["errors"]) and all("per-minute limit" in e for e in run2["errors"]),
      run2["errors"][:1])
check("failed jobs carry the one-shot flag",
      all(j.get("_retried") for j in run2["failed_jobs"]))

# manual Retry (F-B): flag cleared -> a fresh second pass rescues the retry run
ALWAYS_FAIL["on"] = False        # back to odd-fails/even-succeeds
r3 = gen_engine.retry_failed(r2["run_id"])
run3 = wait(r3["run_id"])
check("manual Retry earns a fresh automatic second pass and succeeds",
      run3["ok"] == 2 and run3["errors"] == [], (run3["ok"], run3["errors"]))

gen_engine.generate_one = _real_generate_one

shutil.rmtree(config.OUT_DIR / PROJ, ignore_errors=True)
shutil.rmtree(_TMP, ignore_errors=True)
print()
print(f"{len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
