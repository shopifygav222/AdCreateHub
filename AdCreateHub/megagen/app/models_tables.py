"""Model tables, pricing, durations — ported from bulk_banana_gui.py (Bananza v5.2).

These numbers are battle-tested against the real Vertex API. If Google reprices,
change them HERE and in the stat-cards footnote text in static/index.html.
"""
import re

# List prices per generated image (USD) — Vertex prices may differ slightly
PRICE_PER_IMAGE = {"1K": 0.067, "2K": 0.101, "4K": 0.151}

MODELS = {
    # Nano Banana 2.1 — best-guess ID; swap for the exact Vertex model string
    # from Model Garden if generation returns "model not found".
    "Nano Banana 2.1": "gemini-3.2-flash-image",
    "Nano Banana 2": "gemini-3.1-flash-image",
    "Nano Banana 2 Lite": "gemini-3.1-flash-lite-image",
    "Nano Banana Pro": "gemini-3-pro-image",
}

# "durs" = the clip lengths the model ACTUALLY accepts. Offering 1-10 for
# everything let users pick a length Veo silently refused. Veo 3.x takes 4/6/8,
# Veo 2 takes 5-8; only Omni Flash is free-form (and it falls back to its native
# length by itself if a build rejects the request).
VEO3_DURS = [4, 6, 8]
VEO2_DURS = [5, 6, 7, 8]
OMNI_DURS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]

# "pps" = estimated USD per SECOND of finished video (Google bills video by the
# second, not per clip). List prices as of July 2026.
# kind "veo"  = long-running client.models.generate_videos job (submit, then poll)
# kind "omni" = Interactions API (client.interactions.create) — preview, may not
#               be enabled on Vertex for every project yet; fails with a
#               plain-English hint if so.
# "clip" = fixed clip length in seconds (None = user picks from "durs").
VIDEO_MODELS = {
    "Veo 3.1 · video":      {"id": "veo-3.1-generate-001",      "kind": "veo",  "pps": 0.40, "audio": True,  "clip": None, "durs": VEO3_DURS},
    "Veo 3.1 Fast · video": {"id": "veo-3.1-fast-generate-001", "kind": "veo",  "pps": 0.15, "audio": True,  "clip": None, "durs": VEO3_DURS},
    "Veo 3.1 Lite · video": {"id": "veo-3.1-lite-generate-001", "kind": "veo",  "pps": 0.05, "audio": True,  "clip": None, "durs": VEO3_DURS},
    "Veo 3 · video":        {"id": "veo-3.0-generate-001",      "kind": "veo",  "pps": 0.40, "audio": True,  "clip": None, "durs": VEO3_DURS},
    "Veo 3 Fast · video":   {"id": "veo-3.0-fast-generate-001", "kind": "veo",  "pps": 0.15, "audio": True,  "clip": None, "durs": VEO3_DURS},
    "Veo 2 · video":        {"id": "veo-2.0-generate-001",      "kind": "veo",  "pps": 0.50, "audio": False, "clip": None, "durs": VEO2_DURS},
    # audio: the Interactions API takes no audio flag — gen_video_omni can't send
    # one, so the Sound toggle is hidden for Omni rather than lying to the user.
    #
    # Omni 1.1 Flash is the GA successor to the Omni Flash preview; best-guess ID
    # — swap for the exact Vertex string if generation returns "model not found".
    "Omni 1.1 Flash · video": {"id": "gemini-omni-1-1-flash",   "kind": "omni", "pps": 0.10, "audio": False, "clip": 10, "durs": OMNI_DURS},
    "Omni Flash · video":     {"id": "gemini-omni-flash-preview", "kind": "omni", "pps": 0.10, "audio": False, "clip": 10, "durs": OMNI_DURS},
}

VIDEO_DURATIONS = OMNI_DURS   # widest set — per-model lists come from model["durs"]

VIDEO_RESOLUTIONS = ["720p", "1080p"]
VIDEO_ASPECTS = ("16:9", "9:16")      # the only ratios video models accept
VIDEO_POLL_SECS = 10                  # how often we ask Google "is the video done yet?"
VIDEO_TIMEOUT_SECS = 20 * 60          # give up on one clip after this long

ASPECT_CHOICES = [
    ("✨ auto", "auto"),
    ("◻ 1:1", "1:1"),
    ("▭ 16:9", "16:9"),
    ("▯ 9:16", "9:16"),
    ("▭ 4:3", "4:3"),
    ("▯ 3:4", "3:4"),
    ("▯ 4:5", "4:5"),
    ("▭ 5:4", "5:4"),
    ("▭ 21:9", "21:9"),
    ("▯ 2:3", "2:3"),
    ("▭ 3:2", "3:2"),
]

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTS = (".mp4",)
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS

MAX_RETRIES = 5
TAG_RE = re.compile(r"@(?:image\s*)?(\d+)", re.I)


def model_durs(label):
    """Clip lengths this video model actually accepts."""
    return VIDEO_MODELS.get(label, {}).get("durs") or VIDEO_DURATIONS


def snap_dur(label, value, fallback=8):
    """Clamp a saved/typed length onto the nearest length the model allows.
    Runs on the server no matter what the client sent (stale dropdown, hand-typed
    JSON, model switched between page load and Generate)."""
    allowed = model_durs(label)
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = fallback
    if v in allowed:
        return v
    return min(allowed, key=lambda d: (abs(d - v), d))


def tables_payload():
    """Everything the frontend needs to build dropdowns and price a run, so no
    model or price is ever duplicated in JavaScript."""
    return {
        "price_per_image": PRICE_PER_IMAGE,
        "image_models": list(MODELS.keys()),
        "video_models": {
            label: {
                "kind": m["kind"],
                "pps": m["pps"],
                "audio": m["audio"],
                "clip": m["clip"],
                "durs": m["durs"],
                "veo2": m["id"].startswith("veo-2"),
            }
            for label, m in VIDEO_MODELS.items()
        },
        "aspect_choices": [{"label": lab, "value": val} for lab, val in ASPECT_CHOICES],
        "video_aspects": list(VIDEO_ASPECTS),
        "video_resolutions": list(VIDEO_RESOLUTIONS),
        "image_sizes": ["1K", "2K", "4K"],
        "image_exts": list(IMAGE_EXTS),
        "video_exts": list(VIDEO_EXTS),
    }
