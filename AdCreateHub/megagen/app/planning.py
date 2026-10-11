"""Pure job-planning helpers pulled out of gen_engine.py to keep that file under
~950 lines (§4.9). No shared state — safe to import back wherever needed."""
from . import prompts as prompts_mod
from .config import load_config
from .models_tables import (MODELS, PRICE_PER_IMAGE, VIDEO_ASPECTS, VIDEO_MODELS,
                            VIDEO_RESOLUTIONS, snap_dur)


# ---------------- pre-flight validation (§6.4) ----------------

def validate_images(prompt_list, refs, outputs):
    errors = []
    if not prompt_list:
        errors.append("Import prompts first.")
    errors += prompts_mod.tag_errors(prompt_list, refs)
    if not 1 <= int(outputs) <= 10:
        errors.append("Outputs per prompt must be between 1 and 10.")
    return errors


def validate_videos(prompt_list, frames, outputs, aspect):
    errors = []
    if not prompt_list:
        errors.append("Import prompts first.")
    elif not any(p["n"] in frames for p in prompt_list):
        errors.append("No prompt has a start frame — add frames named like 12.png.")
    if not 1 <= int(outputs) <= 3:
        errors.append("Outputs per prompt must be between 1 and 3.")
    if aspect not in VIDEO_ASPECTS:
        errors.append("Video models only support 16:9 or 9:16.")
    return errors


# ---------------- job builders ----------------

def _image_settings(settings):
    cfg = load_config()
    s = dict(cfg["img"])
    s.update({k: v for k, v in (settings or {}).items() if v is not None})
    if s.get("model") not in MODELS:
        s["model"] = "Nano Banana 2"
    if s.get("size") not in PRICE_PER_IMAGE:
        s["size"] = "1K"
    try:
        s["outputs"] = max(1, min(int(s.get("outputs", 1)), 10))
    except (TypeError, ValueError):
        s["outputs"] = 1
    return s


def _video_settings(settings):
    cfg = load_config()
    s = dict(cfg["vid"])
    s.update({k: v for k, v in (settings or {}).items() if v is not None})
    if s.get("model") not in VIDEO_MODELS:
        s["model"] = "Veo 3.1 Fast · video"
    if s.get("aspect") not in VIDEO_ASPECTS:
        s["aspect"] = "16:9"          # "auto" from config is coerced, not an error
    if s.get("res") not in VIDEO_RESOLUTIONS:
        s["res"] = "720p"
    m = VIDEO_MODELS[s["model"]]
    if m["kind"] == "omni":
        # Omni Flash has no length control in the UI — it always decides its
        # own clip length, so no duration is ever sent for it. The server is
        # the authority here regardless of what a stale client payload sent.
        s["dur"] = None
    else:
        s["dur"] = snap_dur(s["model"], s.get("dur", 8))
    s["audio"] = bool(s.get("audio", True)) and bool(m["audio"])
    try:
        s["outputs"] = max(1, min(int(s.get("outputs", 1)), 3))
    except (TypeError, ValueError):
        s["outputs"] = 1
    return s
