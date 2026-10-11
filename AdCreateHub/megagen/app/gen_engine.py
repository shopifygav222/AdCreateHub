"""Vertex client, image/video generation, retries, the run registry and workers.

The API-facing functions are ported from bulk_banana_gui.py (Bananza v5.2) —
their retry behavior and error strings are battle-tested against the real Vertex
API and are deliberately left alone. Gradio's gr.Error becomes GenError.
"""
import base64
import mimetypes
import random
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED
from concurrent.futures import wait as futures_wait
from pathlib import Path

from . import accounts as accounts_mod
from . import projects, prompts as prompts_mod, thumbs
from .config import load_config
from .errors import humanize
from .models_tables import (MAX_RETRIES, MODELS, PRICE_PER_IMAGE, VIDEO_ASPECTS,
                            VIDEO_MODELS, VIDEO_POLL_SECS, VIDEO_TIMEOUT_SECS)
from .planning import validate_images, validate_videos, _image_settings, _video_settings
from .usage import add_usage


class GenError(Exception):
    """A message meant for the user, not a stack trace."""


_sdk_cache = {}


def _sdk():
    if not _sdk_cache:
        from google import genai
        from google.genai import types
        _sdk_cache["genai"] = genai
        _sdk_cache["types"] = types
    return _sdk_cache["genai"], _sdk_cache["types"]


# ---------------- client ----------------

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


# ---------------- images ----------------

def gen_image(client, model, prompt, image_paths, image_config):
    _, types = _sdk()
    contents = [prompt]
    for p in image_paths:
        data = Path(p).read_bytes()
        mime = mimetypes.guess_type(str(p))[0] or "image/png"
        contents.append(types.Part.from_bytes(data=data, mime_type=mime))

    config = {"response_modalities": ["TEXT", "IMAGE"]}
    if image_config:
        config["image_config"] = dict(image_config)

    try:
        resp = client.models.generate_content(model=model, contents=contents, config=config)
    except Exception as e:
        if "image_size" in str(e) and "image_config" in config:
            config["image_config"].pop("image_size", None)
            if not config["image_config"]:
                config.pop("image_config")
            resp = client.models.generate_content(model=model, contents=contents, config=config)
        else:
            raise

    for cand in (resp.candidates or []):
        for part in (cand.content.parts or []):
            inline = getattr(part, "inline_data", None)
            if inline and inline.data:
                data = inline.data
                return base64.b64decode(data) if isinstance(data, str) else data
    raise RuntimeError("No image in response (safety filter?)")


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


# ---------------- videos ----------------

def _mp4_duration_seconds(path):
    """Clip length straight from the MP4 header (moov→mvhd atom) — no ffmpeg.
    v1.1: reads at most ~4MB from the head and, if needed, ~4MB from the tail
    instead of loading the whole file (moov can sit at either end).
    Used so the spend tracker bills the seconds Google ACTUALLY returned."""
    CHUNK = 4_000_000
    NEED = 36                     # bytes after 'mvhd' the version-1 branch reads
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as fh:
            data = fh.read(CHUNK)
            i = data.find(b"mvhd")
            if (i == -1 or i + NEED > len(data)) and size > CHUNK:
                fh.seek(max(0, size - CHUNK))
                data = fh.read(CHUNK)
                i = data.find(b"mvhd")
        if i == -1 or i + NEED > len(data):
            return None
        if data[i + 4] == 1:  # version 1: 64-bit times
            ts = int.from_bytes(data[i + 24:i + 28], "big")
            dur = int.from_bytes(data[i + 28:i + 36], "big")
        else:                 # version 0: 32-bit times
            ts = int.from_bytes(data[i + 16:i + 20], "big")
            dur = int.from_bytes(data[i + 20:i + 24], "big")
        secs = (dur / ts) if ts else None
        # sanity clamp: a stray "mvhd" in a non-MP4 must never poison billing
        return secs if secs and 0 < secs < 3600 else None
    except Exception:
        return None


def _camel(snake):
    parts = snake.split("_")
    return parts[0] + "".join(w.title() for w in parts[1:])


def _video_bytes_from_op(op):
    resp = getattr(op, "response", None) or getattr(op, "result", None)
    out = []
    for gv in (getattr(resp, "generated_videos", None) or []):
        v = getattr(gv, "video", None)
        b = getattr(v, "video_bytes", None) if v else None
        if isinstance(b, str):
            b = base64.b64decode(b)
        if b:
            out.append(b)
    return out


def gen_video_veo(client, model, prompt, image_path, vcfg):
    """One Veo clip. Video is a long-running job: submit it, then poll until
    Google says it's done. Returns raw MP4 bytes (no GCS bucket needed —
    without output_gcs_uri, Vertex returns the bytes inline)."""
    _, types = _sdk()
    kwargs = {
        "aspect_ratio": vcfg["aspect"],
        "duration_seconds": int(vcfg["dur"]),
        "number_of_videos": 1,
        "resolution": vcfg["res"],
        "generate_audio": bool(vcfg["audio"]),
    }
    if model.startswith("veo-2"):
        kwargs.pop("generate_audio", None)              # Veo 2 has no audio at all
        kwargs.pop("resolution", None)                  # Veo 2 is 720p only
        kwargs["duration_seconds"] = max(5, kwargs["duration_seconds"])  # Veo 2 min = 5s
    image = types.Image.from_file(location=str(image_path)) if image_path else None

    op = None
    for _ in range(4):
        try:
            op = client.models.generate_videos(
                model=model, prompt=prompt, image=image,
                config=types.GenerateVideosConfig(**kwargs))
            break
        except Exception as e:
            # same defensive pattern as gen_image's image_size retry: if this
            # particular model rejects an optional field, drop it and try again.
            msg = str(e)
            bad = next((k for k in ("resolution", "generate_audio", "duration_seconds")
                        if k in kwargs and (k in msg or _camel(k) in msg)), None)
            if bad is None:
                raise
            kwargs.pop(bad)
    if op is None:
        raise RuntimeError("Could not submit the video job")

    t0 = time.time()
    while not getattr(op, "done", False):
        if time.time() - t0 > VIDEO_TIMEOUT_SECS:
            raise RuntimeError("Google took too long making this video (timed out)")
        time.sleep(VIDEO_POLL_SECS)
        op = client.operations.get(op)
    op_err = getattr(op, "error", None)
    if op_err:
        raise RuntimeError(str(getattr(op_err, "message", op_err))[:400])
    vids = _video_bytes_from_op(op)
    if not vids:
        raise RuntimeError("No video in response (safety filter?)")
    return vids[0]


def gen_video_omni(client, model, prompt, image_paths, vcfg, want_dur=True):
    """One Omni Flash clip via the Interactions API. Always includes audio; the
    aspect ratio and the length are configurable. If this Omni build doesn't
    accept a custom duration, we fall back to its native ~10s automatically."""
    if not hasattr(client, "interactions"):
        raise RuntimeError(
            "Your google-genai library is too old for Omni Flash. Close the app, "
            "open a command window, run:  pip install -U google-genai  and start "
            "the app again.")
    if image_paths:
        inp = []
        for ip in image_paths:
            mime = mimetypes.guess_type(str(ip))[0] or "image/png"
            inp.append({"type": "image",
                        "data": base64.b64encode(Path(ip).read_bytes()).decode("ascii"),
                        "mime_type": mime})
        inp.append({"type": "text", "text": prompt})
    else:
        inp = prompt
    fmt = {"type": "video"}
    if vcfg["aspect"] in VIDEO_ASPECTS:
        fmt["aspect_ratio"] = vcfg["aspect"]
    dur = int(vcfg.get("dur") or 0)
    if want_dur and dur:
        fmt["duration_seconds"] = dur
    try:
        interaction = client.interactions.create(model=model, input=inp, response_format=fmt)
    except Exception as e:
        # this Omni build may not accept a custom length — retry once at its
        # native duration rather than failing the whole clip
        if want_dur and dur and "duration" in str(e).lower():
            return gen_video_omni(client, model, prompt, image_paths, vcfg, want_dur=False)
        raise
    ov = getattr(interaction, "output_video", None)
    data = getattr(ov, "data", None) if ov else None
    if isinstance(data, str) and data:
        return base64.b64decode(data)
    if isinstance(data, (bytes, bytearray)) and data:
        return bytes(data)
    for step in (getattr(interaction, "steps", None) or []):   # raw-steps fallback
        for c in (getattr(step, "content", None) or []):
            d = getattr(c, "data", None)
            if getattr(c, "type", "") == "video" and d:
                return base64.b64decode(d) if isinstance(d, str) else bytes(d)
    raise RuntimeError("No video in response (safety filter?)")


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


# ---------------- run registry ----------------

RUNS = {}
RUNS_LOCK = threading.Lock()
DATA_VERSION = 0
_run_counter = iter(range(1, 1_000_000))

_plan_lock = threading.Lock()
_reserved_seqs = {}      # (project, kind, n) -> set of floats still in flight
_planned_names = set()   # (project, basename) planned but not yet on disk
_regen_inflight = set()  # (project, basename)


def bump():
    global DATA_VERSION
    with RUNS_LOCK:
        DATA_VERSION += 1


def data_version():
    with RUNS_LOCK:
        return DATA_VERSION


def _used_seqs(project, kind, n):
    """Every seq already handed out for this (tab, prompt) — on disk and in flight.
    Images and videos keep separate seq spaces: they are separate grids."""
    meta = projects.load_meta(project)
    used = {round(float(v.get("seq", 0.0)), 6) for v in meta.values()
            if isinstance(v, dict) and v.get("kind") == kind and int(v.get("n", -1)) == int(n)}
    used |= _reserved_seqs.get((project, kind, int(n)), set())
    return used


def reserve_seqs(project, kind, n, count):
    """seq = existing max for this prompt (or 0) + 1, +2, … Never renumbers."""
    with _plan_lock:
        used = _used_seqs(project, kind, n)
        base = max(used) if used else 0.0
        out = [round(base + i + 1, 6) for i in range(count)]
        _reserved_seqs.setdefault((project, kind, int(n)), set()).update(out)
        return out


def reserve_seq_after(project, kind, n, seq):
    """"New": land immediately after the original at seq + 0.001·k (§4.3)."""
    with _plan_lock:
        used = _used_seqs(project, kind, n)
        k = 1
        while round(float(seq) + 0.001 * k, 6) in used and k < 999:
            k += 1
        val = round(float(seq) + 0.001 * k, 6)
        _reserved_seqs.setdefault((project, kind, int(n)), set()).add(val)
        return val


def _release_seq(project, kind, n, seq):
    with _plan_lock:
        s = _reserved_seqs.get((project, kind, int(n)))
        if s:
            s.discard(round(float(seq), 6))
            if not s:
                _reserved_seqs.pop((project, kind, int(n)), None)


def claim_name(project, n, kind):
    with _plan_lock:
        taken = {nm for (p, nm) in _planned_names if p == project}
        name = projects.unique_media_name(project, n, kind, random, taken)
        _planned_names.add((project, name))
        return name


def _release_name(project, name):
    with _plan_lock:
        _planned_names.discard((project, name))


def _new_run(kind, project, mode, jobs, bulk=True, replaces=None):
    """`jobs` is the planned job list — its per-prompt counts drive the panel's
    pulsing "…" chips (§9.7)."""
    pending = {}
    for j in jobs:
        pending[int(j["n"])] = pending.get(int(j["n"]), 0) + 1
    run_id = next(_run_counter)
    with RUNS_LOCK:
        RUNS[run_id] = {"id": run_id, "kind": kind, "project": project, "mode": mode,
                        "total": len(jobs), "done": 0, "ok": 0, "errors": [],
                        "failed_jobs": [], "pending": pending,
                        "started": time.time(), "finished": False, "cancel": False,
                        "bulk": bulk, "replaces": replaces}
    return RUNS[run_id]


def runs_payload():
    with RUNS_LOCK:
        runs = [dict(r) for r in RUNS.values()]
    out = []
    for r in runs:
        out.append({
            "id": r["id"], "kind": r["kind"], "project": r["project"], "mode": r["mode"],
            "total": r["total"], "done": r["done"], "ok": r["ok"],
            "errors": r["errors"][:12], "error_count": len(r["errors"]),
            "failed": len(r["failed_jobs"]), "started": r["started"],
            "elapsed": time.time() - r["started"], "finished": r["finished"],
            "cancel": r["cancel"], "bulk": r["bulk"], "replaces": r["replaces"],
            "pending_ns": sorted(r.get("pending", {}).keys()),
        })
    out.sort(key=lambda r: r["id"])
    return out


def cancel_run(run_id):
    with RUNS_LOCK:
        r = RUNS.get(int(run_id))
        if not r:
            raise GenError("That run is gone already.")
        r["cancel"] = True
    bump()
    return True


def dismiss_run(run_id):
    with RUNS_LOCK:
        r = RUNS.get(int(run_id))
        if r and r["finished"]:
            RUNS.pop(int(run_id), None)
    bump()
    return True


# ---------------- job execution ----------------

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


def _finish_job(project, kind, job, path, price, acct):
    """Move the finished file into place, record its recipe, bill it, warm caches."""
    final = Path(path)
    if job.get("replace"):
        # Regenerate: only now — after a confirmed success — does the old file go
        # to Trash and the new one take its exact name and place.
        original = projects.media_dir(project, kind) / job["replace"]
        if original.exists():
            projects.trash_file(project, original)
        thumbs.drop_cache(project, job["replace"])
        dest = projects.media_dir(project, kind) / job["replace"]
        try:
            if dest.exists():
                dest.unlink()
            final.replace(dest)
            final = dest
        except OSError:
            traceback.print_exc()
    info = dict(job["meta"])
    info["ts"] = int(time.time())
    if acct:
        info["account"] = acct
    projects.record_meta(project, final.name, info)
    cost = price(final) if callable(price) else price
    add_usage(1, cost, acct)
    thumbs.build_after_generation(project, final.name, kind)
    return final


# §C7: a job that dies on a transient error (429/500/503) automatically gets
# exactly ONE more try, after waiting out the accounts' rate-limit cooldowns.
SECOND_PASS_MAX_WAIT = 45     # seconds — never cool off longer than this
SECOND_PASS_MIN_WAIT = 10     # seconds — never retry sooner than this


def _cooloff(state, pool):
    """Bounded wait before a job's automatic second try. Sleeps in slices so
    Stop stays responsive; uses this module's `time` name so tests can stub
    the sleeps. Always returns — never blocks past SECOND_PASS_MAX_WAIT."""
    t0 = time.time()
    for _ in range(int(SECOND_PASS_MAX_WAIT / 0.5)):
        if state["cancel"]:
            return
        waited = time.time() - t0
        if waited >= SECOND_PASS_MAX_WAIT:
            return
        if waited >= SECOND_PASS_MIN_WAIT and pool.cooldowns_clear():
            return
        time.sleep(0.5)


def _run_worker(run_id, pool, kind, model_label, jobs, cfg, project, price):
    state = RUNS[run_id]
    multi = pool.enabled_count() > 1
    try:
        with ThreadPoolExecutor(max_workers=pool.capacity()) as ex:
            # submitted in list order (ascending n, then variant) so the pool
            # starts the earliest prompts first
            futures = {ex.submit(_do_job, state, pool, kind, model_label, j, cfg): j
                       for j in jobs}
            pending = set(futures)
            while pending:
                finished, pending = futures_wait(pending, timeout=1.0,
                                                 return_when=FIRST_COMPLETED)
                for fut in finished:
                    job = futures[fut]
                    try:
                        path, e, cancelled, acct = fut.result()
                    except Exception as exc:
                        path, e, cancelled, acct = None, f"{type(exc).__name__}: {exc}", False, None
                    # §C7 automatic second pass: a transient death gets exactly
                    # one resubmission after the rate-limit cooldown, instead of
                    # surfacing an error the user has to retry by hand. The raw
                    # error text is inspected here, BEFORE humanize() (§C2).
                    if (not path and not cancelled and e
                            and not job.get("_retried")
                            and any(m in str(e) for m in accounts_mod.TRANSIENT_MARKERS)):
                        job["_retried"] = True

                        def _second_try(j=job):
                            _cooloff(state, pool)
                            return _do_job(state, pool, kind, model_label, j, cfg)

                        nf = ex.submit(_second_try)
                        futures[nf] = job
                        pending.add(nf)
                        continue
                    if path:
                        try:
                            _finish_job(project, kind, job, path, price, acct)
                        except Exception as exc:
                            traceback.print_exc()
                            path, e = None, f"Saved but couldn't finalise: {exc}"[:300]
                    with RUNS_LOCK:
                        state["done"] += 1
                        left = state["pending"].get(job["n"], 0) - 1
                        if left > 0:
                            state["pending"][job["n"]] = left
                        else:
                            state["pending"].pop(job["n"], None)
                        if path:
                            state["ok"] += 1
                        elif cancelled:
                            state["errors"].append(f"{job['n']} v{job['v']}: cancelled")
                        else:
                            tag = f" [{pool.label_of(acct)}]" if (multi and acct) else ""
                            state["errors"].append(f"{job['n']} v{job['v']}{tag}: {humanize(e)}")
                            if state["bulk"]:
                                state["failed_jobs"].append(job)
                    _release_seq(project, kind, job["n"], job["seq"])
                    if path or cancelled or not state["bulk"]:
                        _release_name(project, Path(job["out"]).name)
                    bump()
    except Exception as e:
        traceback.print_exc()
        with RUNS_LOCK:
            state["errors"].append(humanize(f"{type(e).__name__}: {e}"))
    finally:
        with RUNS_LOCK:
            state["finished"] = True
            state["pending"] = {}
            done_ids = [i for i, r in RUNS.items() if r["finished"]]
            for i in done_ids[:-4]:          # keep the 4 most recent finished runs
                RUNS.pop(i, None)
        bump()


def _launch(run, pool, kind, model_label, jobs, cfg, price):
    threading.Thread(
        target=_run_worker,
        args=(run["id"], pool, kind, model_label, jobs, cfg, run["project"], price),
        daemon=True).start()
    bump()


# ---------------- pre-flight validation & job builders ----------------
# validate_images, validate_videos, _image_settings, _video_settings live in
# app/planning.py (pure helpers, no shared state — moved out to keep this file
# under ~950 lines, §4.9).

def _make_pool(run, kind):
    """One pool per run. Video LROs stay capped at 2 per account (the ported
    quota rule); images use the workers slider per account."""
    workers = int(load_config().get("workers", 3))
    cap = min(workers, 2) if kind == "video" else workers
    return accounts_mod.AccountPool(cap, cancel_check=lambda: run["cancel"])


def start_images(project, settings):
    s = _image_settings(settings)
    plist = prompts_mod.load_prompts(project, "images")
    refs = prompts_mod.list_refs(project)
    errors = validate_images(plist, refs, s["outputs"])
    if errors:
        raise GenError(errors)
    if not accounts_mod.enabled_accounts():
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")

    image_config = {"image_size": s["size"]}
    if s["aspect"] != "auto":          # "auto" means omit the field, never send it
        image_config["aspect_ratio"] = s["aspect"]

    out_dir = projects.images_dir(project)
    jobs = []
    for p in plist:
        n = p["n"]
        slot_refs = refs.get(n, [])
        ref_names = [r["name"] for r in slot_refs]
        ref_paths = [str(projects.refs_dir(project) / r) for r in ref_names]
        final_text = prompts_mod.build_prompt_final(p["text"], len(ref_names))
        seqs = reserve_seqs(project, "image", n, s["outputs"])
        for v in range(s["outputs"]):
            name = claim_name(project, n, "image")
            jobs.append({
                "n": n, "v": v + 1, "seq": seqs[v], "prompt_final": final_text,
                "refs": ref_paths, "out": str(out_dir / name), "replace": None,
                "meta": {"kind": "image", "n": n, "seq": seqs[v], "prompt": p["text"],
                         "refs": ref_names, "model": s["model"], "aspect": s["aspect"],
                         "size": s["size"]},
            })

    mode = f"Images · {len(plist)} prompts × {s['outputs']}"
    run = _new_run("image", project, mode, jobs)
    pool = _make_pool(run, "image")
    _launch(run, pool, "image", s["model"], jobs, image_config,
            PRICE_PER_IMAGE.get(s["size"], 0.067))
    return {"run_id": run["id"], "total": len(jobs), "skipped": []}


def start_videos(project, settings):
    s = _video_settings(settings)
    plist = prompts_mod.load_prompts(project, "videos")
    frames = prompts_mod.list_frames(project)
    errors = validate_videos(plist, frames, s["outputs"], s["aspect"])
    if errors:
        raise GenError(errors)
    if not accounts_mod.enabled_accounts():
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")

    m = VIDEO_MODELS[s["model"]]
    dur = int(s["dur"]) if s["dur"] is not None else None   # None only for Omni Flash
    vcfg = {"aspect": s["aspect"], "res": s["res"], "dur": dur, "audio": bool(s["audio"])}
    out_dir = projects.videos_dir(project)

    jobs, skipped, matched = [], [], 0
    for p in plist:
        n = p["n"]
        frame = frames.get(n)
        if not frame:
            skipped.append(n)          # never text-to-video — skipped, not an error
            continue
        matched += 1
        frame_path = str(projects.frames_dir(project) / frame)
        final_text = prompts_mod.build_prompt_final(p["text"], 0)
        seqs = reserve_seqs(project, "video", n, s["outputs"])
        for v in range(s["outputs"]):
            name = claim_name(project, n, "video")
            jobs.append({
                "n": n, "v": v + 1, "seq": seqs[v], "prompt_final": final_text,
                "refs": [frame_path], "out": str(out_dir / name), "replace": None,
                "meta": {"kind": "video", "n": n, "seq": seqs[v], "prompt": p["text"],
                         "frame": frame, "model": s["model"], "aspect": s["aspect"],
                         "res": s["res"], "dur": dur, "audio": bool(s["audio"])},
            })

    # Omni Flash has no requested length to bill against if the real duration
    # can't be read back from the file — fall back to its native clip length.
    pps, fallback = m["pps"], (dur if dur is not None else int(m.get("clip") or 10))

    def price(dst):
        return pps * (_mp4_duration_seconds(dst) or fallback)

    mode = f"Videos · {matched} prompts × {s['outputs']}"
    run = _new_run("video", project, mode, jobs)
    pool = _make_pool(run, "video")
    _launch(run, pool, "video", s["model"], jobs, vcfg, price)
    return {"run_id": run["id"], "total": len(jobs), "skipped": skipped}


# ---------------- regenerate / new / edit (§6.5, §6.6) ----------------

def _recipe_env(project, name, recipe):
    """Rebuild (kind, n, out_dir, inputs, model_label, cfg, price) from a saved
    recipe — shared by Regenerate, New and Edit. Raises GenError with the same
    battle-tested messages when a ref/frame was deleted or the model is unknown."""
    kind = recipe.get("kind") or ("video" if name.lower().endswith(".mp4") else "image")
    n = int(recipe.get("n", 0))
    out_dir = projects.media_dir(project, kind)

    if kind == "video":
        # the live frames/ folder first, then the copy preserved on delete
        fpath = projects.input_path(project, recipe.get("frame"), "frames")
        if not fpath:
            raise GenError("A reference this item used was deleted.")
        inputs = [str(fpath)]
        model_label = recipe.get("model")
        if model_label not in VIDEO_MODELS:
            raise GenError("That item used a video model this build doesn't know.")
        vm = VIDEO_MODELS[model_label]
        # Omni Flash items were made with no requested duration — replay stays
        # consistent with that and lets Omni decide again, same as a fresh run.
        recipe_dur = recipe.get("dur")
        cfg_dur = None if vm["kind"] == "omni" else int(recipe_dur if recipe_dur is not None else 8)
        cfg = {"aspect": recipe.get("aspect", "16:9"), "res": recipe.get("res", "720p"),
               "dur": cfg_dur, "audio": bool(recipe.get("audio", True))}
        pps = vm["pps"]
        fallback = cfg_dur if cfg_dur is not None else int(vm.get("clip") or 10)

        def price(dst):
            return pps * (_mp4_duration_seconds(dst) or fallback)
    else:
        ref_names = list(recipe.get("refs") or [])
        inputs = []
        for r in ref_names:
            p = projects.input_path(project, r, "refs")
            if not p:
                raise GenError("A reference this item used was deleted.")
            inputs.append(str(p))
        model_label = recipe.get("model")
        if model_label not in MODELS:
            raise GenError("That item used an image model this build doesn't know.")
        cfg = {"image_size": recipe.get("size", "1K")}
        if recipe.get("aspect") and recipe["aspect"] != "auto":
            cfg["aspect_ratio"] = recipe["aspect"]
        price = PRICE_PER_IMAGE.get(recipe.get("size"), 0.067)

    return kind, n, out_dir, inputs, model_label, cfg, price


def _replay_job(project, name, recipe, mode_out):
    """Shared setup for Regenerate and New: rebuild the exact request that made
    this file. mode_out: "replace" | "new"."""
    kind, n, out_dir, inputs, model_label, cfg, price = _recipe_env(project, name, recipe)

    meta = dict(recipe)
    if mode_out == "replace":
        seq = float(recipe.get("seq", 0.0))
        out_path = out_dir / f".{name}.regen.tmp"
        replace = name
        out_name = name
    else:
        seq = reserve_seq_after(project, kind, n, recipe.get("seq", 0.0))
        out_name = claim_name(project, n, kind)
        out_path = out_dir / out_name
        replace = None
    meta["seq"] = seq
    meta["n"] = n
    meta["kind"] = kind

    job = {"n": n, "v": 1, "seq": seq, "prompt_final": prompts_mod.build_prompt_final(
        recipe.get("prompt", ""), len(inputs) if kind == "image" else 0),
        "refs": inputs, "out": str(out_path), "replace": replace, "meta": meta}
    return kind, model_label, cfg, price, job, out_name


def regenerate(project, name):
    name = Path(str(name)).name
    recipe = projects.meta_for(project, name)
    if not recipe:
        raise GenError("No saved recipe for this item.")
    if not accounts_mod.enabled_accounts():
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")
    with _plan_lock:
        if (project, name) in _regen_inflight:
            raise GenError("Already regenerating this item.")
        _regen_inflight.add((project, name))
    try:
        kind, model_label, cfg, price, job, _ = _replay_job(project, name, recipe, "replace")
    except Exception:
        with _plan_lock:
            _regen_inflight.discard((project, name))
        raise
    run = _new_run(kind, project, f"Regenerate {name}", [job], bulk=False, replaces=name)
    pool = _make_pool(run, kind)

    def _watch():
        _run_worker(run["id"], pool, kind, model_label, [job], cfg, project, price)
        with _plan_lock:
            _regen_inflight.discard((project, name))

    threading.Thread(target=_watch, daemon=True).start()
    bump()
    return {"run_id": run["id"], "total": 1}


def new_from(project, name):
    name = Path(str(name)).name
    recipe = projects.meta_for(project, name)
    if not recipe:
        raise GenError("No saved recipe for this item.")
    if not accounts_mod.enabled_accounts():
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")
    kind, model_label, cfg, price, job, out_name = _replay_job(project, name, recipe, "new")
    run = _new_run(kind, project, f"New from {name}", [job], bulk=False)
    pool = _make_pool(run, kind)
    _launch(run, pool, kind, model_label, [job], cfg, price)
    return {"run_id": run["id"], "total": 1, "name": out_name}


def edit_from(project, name, new_prompt, outputs):
    """✏️ Edit: same recipe (model/settings/refs/frame), user-supplied prompt,
    1..10 copies (images) or exactly 1 (videos), each landing right after the
    source via the same seq+0.001 rule as New. Never touches the prompt list."""
    name = Path(str(name)).name
    recipe = projects.meta_for(project, name)
    if not recipe:
        raise GenError("No saved recipe for this item.")
    text = (new_prompt or "").strip()
    if not text:
        raise GenError("Type a prompt first.")
    if not accounts_mod.enabled_accounts():
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")
    kind, n, out_dir, inputs, model_label, cfg, price = _recipe_env(project, name, recipe)

    if kind == "image":
        k = len(recipe.get("refs") or [])
        for j in prompts_mod.prompt_tags(text):
            if j < 1 or j > k:
                raise GenError(f"The prompt uses @{j} but this item has only {k} reference(s).")
        try:
            count = max(1, min(int(outputs), 10))
        except (TypeError, ValueError):
            count = 1
    else:
        count = 1

    prompt_final = prompts_mod.build_prompt_final(text, len(inputs) if kind == "image" else 0)
    jobs, base = [], recipe.get("seq", 0.0)
    for v in range(count):
        seq = reserve_seq_after(project, kind, n, base)
        base = seq                       # chain: outputs stay adjacent and ordered
        out_name = claim_name(project, n, kind)
        meta = dict(recipe)
        meta.update({"prompt": text, "seq": seq, "n": n, "kind": kind})
        jobs.append({"n": n, "v": v + 1, "seq": seq, "prompt_final": prompt_final,
                     "refs": inputs, "out": str(out_dir / out_name),
                     "replace": None, "meta": meta})

    mode = f"Edit {name}" + (f" × {count}" if count > 1 else "")
    run = _new_run(kind, project, mode, jobs, bulk=(count > 1))
    pool = _make_pool(run, kind)
    _launch(run, pool, kind, model_label, jobs, cfg, price)
    return {"run_id": run["id"], "total": len(jobs)}


# ---------------- retry failed (§6.7) ----------------

def retry_failed(run_id):
    with RUNS_LOCK:
        old = RUNS.get(int(run_id))
        if not old:
            raise GenError("That run is gone already.")
        if not old["bulk"]:
            raise GenError("Regenerate and New runs don't have a retry — use the tile buttons.")
        jobs = list(old["failed_jobs"])
        old["failed_jobs"] = []
        project, kind = old["project"], old["kind"]
    if not jobs:
        raise GenError("Nothing failed in that run.")
    if not accounts_mod.enabled_accounts():
        raise GenError("No Project ID set — open ⚙ Settings, or click Guide if it's your first time.")

    cfg_all = load_config()
    if kind == "video":
        model_label = jobs[0]["meta"].get("model")
        m = VIDEO_MODELS.get(model_label)
        if not m:
            raise GenError("That run used a video model this build doesn't know.")
        meta0 = jobs[0]["meta"]
        meta_dur = meta0.get("dur")
        cfg_dur = None if m["kind"] == "omni" else int(meta_dur if meta_dur is not None else 8)
        cfg = {"aspect": meta0.get("aspect", "16:9"), "res": meta0.get("res", "720p"),
               "dur": cfg_dur, "audio": bool(meta0.get("audio", True))}
        pps = m["pps"]
        fallback = cfg_dur if cfg_dur is not None else int(m.get("clip") or 10)

        def price(dst):
            return pps * (_mp4_duration_seconds(dst) or fallback)
    else:
        meta0 = jobs[0]["meta"]
        model_label = meta0.get("model")
        if model_label not in MODELS:
            raise GenError("That run used an image model this build doesn't know.")
        cfg = {"image_size": meta0.get("size", "1K")}
        if meta0.get("aspect") and meta0["aspect"] != "auto":
            cfg["aspect_ratio"] = meta0["aspect"]
        price = PRICE_PER_IMAGE.get(meta0.get("size"), 0.067)

    # the planned output names were never created — reuse them exactly
    with _plan_lock:
        for j in jobs:
            j.pop("_retried", None)   # a manual Retry earns a fresh §C7 second pass
            _reserved_seqs.setdefault((project, kind, int(j["n"])), set()).add(round(float(j["seq"]), 6))

    run = _new_run(kind, project, f"Retry {len(jobs)} failed", jobs)
    pool = _make_pool(run, kind)
    _launch(run, pool, kind, model_label, jobs, cfg, price)
    return {"run_id": run["id"], "total": len(jobs)}


# ---------------- delete ----------------

def delete_media(project, names):
    gone = 0
    for name in names or []:
        p = projects.find_media(project, name)
        if not p:
            continue
        thumbs.drop_cache(project, p.name)
        projects.trash_file(project, p)
        gone += 1
    bump()
    return gone
