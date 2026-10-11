"""Omni Flash has no length control — it always picks its own clip length.
Every other video model still requires an explicit, user-chosen duration.

Stubbed at the generate_one_video boundary — no real accounts or API calls
needed, and isolated from the user's real config.json/usage.json."""
import shutil, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image
from app import accounts as A, config, gen_engine, planning, projects, prompts as pm, usage

_TMP = Path(tempfile.mkdtemp(prefix="megagen-durtest-"))
config.CONFIG_FILE = _TMP / "config.json"
config.USAGE_FILE = _TMP / "usage.json"
usage.USAGE_FILE = config.USAGE_FILE

# start_videos requires at least one enabled account before it builds jobs
A.save_accounts([{"id": "acc1", "label": "One", "project": "p-one", "location": "global",
                  "auth": "adc", "credit": 300.0, "enabled": True}])

PROJ = "DurTest"
SEEN = []          # (model_label, vcfg) for every stubbed call


def stub_video(pool, model_label, prompt, image_paths, out_path, vcfg):
    SEEN.append((model_label, dict(vcfg)))
    Path(out_path).write_bytes(b"\x00")     # not a real mp4 — _mp4_duration_seconds -> None
    return str(out_path), None, "acc1"
gen_engine.generate_one_video = stub_video

ok, bad = [], []
def check(label, cond, detail=""):
    (ok if cond else bad).append(label)
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  -> " + str(detail)) if detail else ""))


def wait(run_id, limit=15):
    t0 = time.time()
    while time.time() - t0 < limit:
        r = gen_engine.RUNS.get(run_id)
        if r and r["finished"]:
            return r
        time.sleep(0.02)
    raise AssertionError("run did not finish")


def media():
    return projects.list_media(PROJ, "videos")


d = config.OUT_DIR / PROJ
if d.exists():
    shutil.rmtree(d)
projects.project_dir(PROJ)


def only_prompt(n, text):
    """Isolate a run to exactly one prompt, so runs never cross-contaminate."""
    pm.save_prompts(PROJ, "videos", [{"n": n, "text": text}])
    Image.new("RGB", (64, 64), (20, 60, 90)).save(projects.frames_dir(PROJ) / f"{n}.png")


# ---- 1. _video_settings: Omni forces dur=None, Veo still snaps (regression) ----
s_omni = planning._video_settings({"model": "Omni Flash · video", "dur": 6})
check("Omni Flash settings force dur=None regardless of requested value",
      s_omni["dur"] is None, s_omni["dur"])
s_veo = planning._video_settings({"model": "Veo 3.1 Fast · video", "dur": 3})
check("Veo settings still snap to a valid duration (regression)",
      s_veo["dur"] == 4, s_veo["dur"])   # 3 snaps to nearest of [4,6,8] -> 4

# ---- 2. start_videos: Omni run sends no duration ----
only_prompt(1, "omni clip")
r1 = gen_engine.start_videos(PROJ, {"model": "Omni Flash · video", "outputs": 1})
wait(r1["run_id"])
omni_call = SEEN[-1]
check("Omni Flash job carries no dur in vcfg", omni_call[1]["dur"] is None, omni_call)
omni_item = [i for i in media() if i["n"] == 1][0]
check("Omni Flash recipe stores dur=None", omni_item["meta"]["dur"] is None, omni_item["meta"])

# ---- 3. start_videos: Veo run still sends an explicit duration (regression) ----
only_prompt(2, "veo clip")
SEEN.clear()
r2 = gen_engine.start_videos(PROJ, {"model": "Veo 3.1 Fast · video", "dur": 8, "outputs": 1})
wait(r2["run_id"])
veo_call = SEEN[-1]
check("Veo job still carries an explicit int dur (regression)",
      veo_call[1]["dur"] == 8, veo_call)
veo_item = [i for i in media() if i["n"] == 2][0]
check("Veo recipe still stores the explicit dur (regression)",
      veo_item["meta"]["dur"] == 8, veo_item["meta"])

# ---- 4. billing fallback: Omni bills against its native clip (10s), Veo against its request ----
u = usage.load_usage()
omni_pps = gen_engine.VIDEO_MODELS["Omni Flash · video"]["pps"]
veo_pps = gen_engine.VIDEO_MODELS["Veo 3.1 Fast · video"]["pps"]
expected = omni_pps * 10 + veo_pps * 8   # neither stub wrote a real mp4 header -> fallback used
check("billed using the model's native/requested length as fallback",
      abs(u["est_cost"] - expected) < 0.01, (u["est_cost"], expected))

# ---- 5. Regenerate/New/Edit replay stays consistent per model ----
SEEN.clear()
rr = gen_engine.regenerate(PROJ, omni_item["name"])
wait(rr["run_id"])
check("Regenerate of an Omni item sends no duration again",
      SEEN[-1][1]["dur"] is None, SEEN[-1])

SEEN.clear()
rr2 = gen_engine.regenerate(PROJ, veo_item["name"])
wait(rr2["run_id"])
check("Regenerate of a Veo item still sends its explicit duration (regression)",
      SEEN[-1][1]["dur"] == 8, SEEN[-1])

SEEN.clear()
rn = gen_engine.new_from(PROJ, omni_item["name"])
wait(rn["run_id"])
check("New from an Omni item sends no duration",
      SEEN[-1][1]["dur"] is None, SEEN[-1])

SEEN.clear()
rn2 = gen_engine.new_from(PROJ, veo_item["name"])
wait(rn2["run_id"])
check("New from a Veo item still sends its explicit duration (regression)",
      SEEN[-1][1]["dur"] == 8, SEEN[-1])

SEEN.clear()
re_ = gen_engine.edit_from(PROJ, omni_item["name"], "edited omni prompt", 1)
wait(re_["run_id"])
check("Edit of an Omni item sends no duration",
      SEEN[-1][1]["dur"] is None, SEEN[-1])
edited = [i for i in media() if i["n"] == 1 and i["seq"] > omni_item["seq"]][0]
check("Edited Omni item's recipe also stores dur=None",
      edited["meta"]["dur"] is None, edited["meta"])

SEEN.clear()
re2 = gen_engine.edit_from(PROJ, veo_item["name"], "edited veo prompt", 1)
wait(re2["run_id"])
check("Edit of a Veo item still sends its explicit duration (regression)",
      SEEN[-1][1]["dur"] == 8, SEEN[-1])

# ---- 6. retry_failed stays consistent per model ----
def fail_once(pool, model_label, prompt, image_paths, out_path, vcfg):
    SEEN.append((model_label, dict(vcfg)))
    return None, "RuntimeError: stubbed failure", "acc1"
gen_engine.generate_one_video = fail_once

only_prompt(3, "omni retry")
rf = gen_engine.start_videos(PROJ, {"model": "Omni Flash · video", "outputs": 1})
run_f = wait(rf["run_id"])
check("failed run recorded for retry", len(run_f["failed_jobs"]) == 1, run_f["failed_jobs"])

gen_engine.generate_one_video = stub_video
SEEN.clear()
rt = gen_engine.retry_failed(rf["run_id"])
wait(rt["run_id"])
check("retry of a failed Omni job still sends no duration",
      SEEN[-1][1]["dur"] is None, SEEN[-1])

shutil.rmtree(config.OUT_DIR / PROJ, ignore_errors=True)
shutil.rmtree(_TMP, ignore_errors=True)
print()
print(f"{len(ok)} passed, {len(bad)} failed")
sys.exit(1 if bad else 0)
