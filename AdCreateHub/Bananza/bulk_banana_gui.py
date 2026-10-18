"""
bulk_banana_gui.py — Bananza 🍌 (Nano Banana bulk generator, Flow-style layout)

Built to run on the Google Cloud $300 free trial via Agent Platform (Vertex).
No API key needed — one-time `gcloud` sign-in plus a Project ID.
Click "Setup guide" in the app for beginner-proof instructions.

Adding reference images: Ctrl+V, drag & drop, the paste tile, or Add images.

Modes (picked automatically):
  • No reference images  -> text-to-image (Outputs = how many variations)
  • Reference images     -> prompt applied to every image in the tray
  • @1 / @2 in prompt    -> combines the tagged tray images in one generation

Generation runs in the background: press Generate as many times as you like,
runs stack, and images pop into the canvas the moment each one finishes.

Selecting images in the canvas:
  • Drag across thumbnails to lasso-select several at once
  • Ctrl+click toggles one image, Shift+click selects a range
  • Ctrl+A selects everything, Esc clears, Del deletes the selection
  • Ctrl+C (one image selected) copies it to the clipboard

Keyboard shortcuts (desktop) — Alt+key works even while typing a prompt:
  • Enter (in the prompt) or Ctrl+Enter (anywhere) = Generate; Shift+Enter = new line
  • Alt+S  open/close the generation settings   (Ctrl+, does the same)
  • Alt+`  jump into the prompt box / back out  ( / also jumps in)
  • Alt+L  video Length (Seconds) dropdown
  • Alt+C  Clear all      Alt+Z  put the cleared prompt back
  If the shortcuts ever go quiet, the browser (not the app) has taken keyboard
  focus — click anywhere on the page once and they work again.

Projects: every generation lands in the project folder picked at the top.
Deleted images/projects go to output/.trash — recoverable, never destroyed.

Setup (once):
    pip install -U google-genai gradio

Run:
    python bulk_banana_gui.py
    python bulk_banana_gui.py --share    (adds a temporary public link for phones/friends)
"""

import base64
import hashlib
import html
import inspect
import logging
import mimetypes
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from pathlib import Path

# The heavy imports below (gradio + google-genai) take several seconds. Put
# something on screen IMMEDIATELY so double-clicking the script never shows a
# silent black window — main() clears this and draws the real banner.
if __name__ == "__main__":
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW("Bananza — loading…")
        print("\n  BANANZA is waking up — give it a few seconds...\n", flush=True)
    except Exception:
        pass

import gradio as gr
from google import genai
from google.genai import types


class _MuteDisconnectNoise(logging.Filter):
    """Windows asyncio logs a scary traceback every time the browser drops an
    HTTP connection mid-flight (tab closed, page refreshed, poll aborted):
    'ConnectionResetError: [WinError 10054] ... forcibly closed'. It's harmless
    but floods the friendly console — drop just those records."""

    _NEEDLES = ("ConnectionResetError", "WinError 10054", "_call_connection_lost",
                "ProactorBasePipeTransport")

    def filter(self, record):
        try:
            if record.exc_info and record.exc_info[0] is ConnectionResetError:
                return False
            msg = record.getMessage()
            return not any(n in msg for n in self._NEEDLES)
        except Exception:
            return True


for _name in ("asyncio", "uvicorn", "uvicorn.error"):
    logging.getLogger(_name).addFilter(_MuteDisconnectNoise())

APP_VERSION = "6.5"

# List prices per generated image (USD) — Vertex prices may differ slightly
PRICE_PER_IMAGE = {"1K": 0.067, "2K": 0.101, "4K": 0.151}

MODELS = {
    # Nano Banana 2.1 — GA on Vertex since 2026-10-06.
    "Nano Banana 2.1": "gemini-nano-banana-2.1",
    # Nano Banana 2 (gemini-3.1-flash-image) is scheduled to shut down
    # 2026-10-29; kept here until then so saved config keeps working.
    "Nano Banana 2": "gemini-3.1-flash-image",
    "Nano Banana 2 Lite": "gemini-3.1-flash-lite-image",
    "Nano Banana Pro": "gemini-3-pro-image",
}

# ---- video models (v3.9) ----
# "pps" = estimated USD per SECOND of finished video (Google bills video by the
# second, not per clip). List prices as of July 2026 — if Google repriced,
# adjust here AND in the stat-cards footnote text below.
# kind "veo"  = long-running client.models.generate_videos job (submit, then poll)
# kind "omni" = Interactions API (client.interactions.create) — preview, may not
#               be enabled on Vertex for every project yet; fails with a
#               plain-English hint if so.
# "clip" = fixed clip length in seconds (None = user picks 4/6/8).
# "durs" = the clip lengths the model ACTUALLY accepts (v6.0). Offering 1-10 for
# everything let users pick a length Veo silently refused. Veo 3.x takes 4/6/8,
# Veo 2 takes 5-8; only Omni Flash is free-form (and it falls back to its native
# length by itself if a build rejects the request).
VEO3_DURS = [4, 6, 8]
VEO2_DURS = [5, 6, 7, 8]
OMNI_DURS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
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
    # Omni 1.1 Flash: GA on the Gemini API since 2026-08-27.
    "Omni 1.1 Flash · video": {"id": "gemini-omni-1.1-flash",   "kind": "omni", "pps": 0.10, "audio": False, "clip": 10, "durs": OMNI_DURS},
    # Omni Flash (preview) was deprecated 2026-09-30 — keep only if a saved
    # config still names it; new picks should use the 1.1 Flash entry above.
    "Omni Flash · video":     {"id": "gemini-omni-flash-preview", "kind": "omni", "pps": 0.10, "audio": False, "clip": 10, "durs": OMNI_DURS},
}
VIDEO_DURATIONS = OMNI_DURS   # widest set — per-model lists come from model["durs"]


def model_durs(label):
    """Clip lengths this video model actually accepts."""
    return VIDEO_MODELS.get(label, {}).get("durs") or VIDEO_DURATIONS


def snap_dur(label, value, fallback=8):
    """Clamp a saved/typed length onto the nearest length the model allows."""
    allowed = model_durs(label)
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = fallback
    if v in allowed:
        return v
    return min(allowed, key=lambda d: (abs(d - v), d))
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
COUNTS = [1, 2, 3, 4, 6, 8, 10]
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
VIDEO_EXTS = (".mp4",)
MEDIA_EXTS = IMAGE_EXTS + VIDEO_EXTS
MEDIA_FILTERS = ["All", "Images", "Videos"]   # canvas filter (v4.0)
UNSORTED = "Unsorted"

BASE_DIR = Path(__file__).parent
OUT_DIR = BASE_DIR / "output"
TRASH_DIR = OUT_DIR / ".trash"
USAGE_FILE = BASE_DIR / "usage.json"
CONFIG_FILE = BASE_DIR / "config.json"
MAX_RETRIES = 5
TOAST_SECONDS = 8

TAG_RE = re.compile(r"@(?:image\s*)?(\d+)", re.I)

# ---- background run registry (lets generations stack) ----
RUNS = {}                      # run_id -> state dict
RUNS_LOCK = threading.Lock()
DATA_VERSION = 0               # bumped whenever anything changes on disk
SESSION = {"images": 0, "cost": 0.0}
_run_counter = iter(range(1, 10_000))
_usage_lock = threading.Lock()

ACCENT = "139,92,246"  # violet

CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Outfit:wght@700;800&display=swap');

/* ---------- global look ---------- */
.gradio-container {
    /* v4.7 density pass: Flow's chrome is smaller than Gradio's defaults —
       shrink the type scale ~12% so the whole app reads "zoomed out" */
    font-size: 14px;
    --text-sm: 11px;
    --text-md: 13px;
    --text-lg: 15px;
    --block-radius: 12px;
    --button-large-radius: 11px;
    --button-small-radius: 9px;
    /* Flow look: neutral chrome borders on surfaces; violet is reserved for
       interaction (focus / hover / selection / the Generate button). */
    --block-border-color: rgba(255,255,255,.07);
    --input-border-color-focus: rgba(167,139,250,.65);
    --loader-color: #a78bfa;
    /* Flow's near-black canvas with only a whisper of violet at the very top */
    background:
        radial-gradient(1200px 620px at 50% -240px, rgba(139,92,246,.10), transparent 72%),
        #0e0e11 !important;
}
.gradio-container > .main {padding: 6px 14px 4px 14px !important;}
::-webkit-scrollbar {width: 8px; height: 8px;}
::-webkit-scrollbar-thumb {background: rgba(139,92,246,.35); border-radius: 8px;}
::-webkit-scrollbar-thumb:hover {background: rgba(139,92,246,.6);}
::-webkit-scrollbar-track {background: transparent;}
button {transition: filter .12s ease, transform .12s ease, box-shadow .12s ease !important;}
button:hover {filter: brightness(1.12);}
button:active {transform: scale(.985);}
button.sm {border-radius: 10px !important;}
h1.app-title {
    margin: 0;
    font-family: 'Outfit', Inter, sans-serif;
    font-size: 1.45rem;
    background: linear-gradient(90deg, #c4b5fd, #a78bfa 30%, #f472b6 55%, #a78bfa 80%, #c4b5fd);
    background-size: 250% 100%;
    -webkit-background-clip: text;
    background-clip: text;
    color: transparent;
    font-weight: 800;
    letter-spacing: -0.5px;
    white-space: nowrap;
    animation: title-sheen 9s ease-in-out infinite;
}
@keyframes title-sheen {
    0%, 100% {background-position: 0% 0;}
    50% {background-position: 100% 0;}
}
#topbar, #projectbar {align-items: center; gap: 8px !important;}
#topstats {text-align: right; font-size: .8rem; opacity: .85; white-space: nowrap;}
#topstats a {color: #a78bfa;}

/* ---------- project bar ---------- */
#projectbar {
    border-radius: 14px;
    padding: 6px 10px;
    border: 1px solid rgba(139,92,246,.14);
    background: color-mix(in srgb, var(--block-background-fill) 65%, transparent);
}
/* v6.4: the confirm dialogs now follow the SAME shape as #setup-wrap /
   #settings-wrap — an elem_id wrapper that only positions, and an elem_id box
   inside that only decorates. See the block further down for why that matters. */

/* ---------- canvas & gallery ---------- */
#canvas-card {
    position: relative;
    border-radius: 18px !important;
    padding: 8px 10px !important;
    border: 1px solid rgba(139,92,246,.16) !important;
    background: color-mix(in srgb, var(--block-background-fill) 88%, transparent) !important;
}
#result-gallery .grid-wrap {position: relative; overflow-y: auto;}  /* height pinned in the v4.8 rule below */
/* rows size to content and cards keep their 3:4 shape — every grid size (2-6)
   renders proportional cards instead of stretching to fill 58vh */
#result-gallery .grid-container {
    gap: 8px !important;
    grid-template-rows: none !important;
    grid-auto-rows: max-content !important;
    align-content: start;
}
#result-gallery .gallery-item {width: 100%; height: auto !important;}
#result-gallery img {-webkit-user-drag: none;}
/* Each tile takes ITS image's true aspect ratio, so the image fills the full
   column WIDTH with no black side-bars and nothing is cropped. Portrait shots
   just make taller tiles (scroll for the rest); use a higher grid size for
   shorter tiles. (Fixed-3:4 tiles side-letterboxed tall 9:16 shots badly.) */
#result-gallery .thumbnail-item {
    position: relative;
    width: 100%;
    height: auto;
    border-radius: 12px !important;
    overflow: hidden;
    border: none !important;
    background: #0b0b0d !important;
    transition: box-shadow .18s ease;
}
#result-gallery .thumbnail-item img,
#result-gallery .thumbnail-item video {
    display: block;
    width: 100%;
    height: auto;
    object-fit: contain;
    padding: 0;
    transition: transform .25s ease;
}
#result-gallery .thumbnail-item:hover img,
#result-gallery .thumbnail-item:hover video {transform: scale(1.045);}
#result-gallery .thumbnail-item:hover {box-shadow: 0 0 0 1px rgba(167,139,250,.35), 0 12px 32px rgba(0,0,0,.5);}
#result-gallery .thumbnail-item.selected {
    box-shadow: 0 0 0 2px rgba(167,139,250,.55), 0 10px 26px rgba(139,92,246,.3);
}

/* hover action buttons (reuse prompt / add as ingredient / delete), Flow-style */
.thumb-actions {
    position: absolute;
    top: 8px;
    right: 8px;
    display: flex;
    gap: 6px;
    z-index: 25;
    opacity: 0;
    pointer-events: none;
    transition: opacity .15s ease;
}
.thumbnail-item:hover > .thumb-actions, .thumb-actions:hover {opacity: 1; pointer-events: auto;}
body.ms-dragging .thumb-actions {opacity: 0 !important; pointer-events: none !important;}
.thumb-actions button {
    width: 27px;
    height: 27px;
    border-radius: 999px;
    border: none;
    /* v6.0 PERF: solid instead of blurred — 4 buttons × every hovered tile */
    background: rgba(15,15,18,.88);
    color: #eee;
    font-size: 13px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 10px rgba(0,0,0,.4);
}
.thumb-actions button:hover {background: rgba(139,92,246,.9); color: #fff;}

/* ---------- multi-select (drag lasso + Ctrl/Shift click) ---------- */
body.ms-dragging {user-select: none !important; cursor: crosshair !important;}
body.ms-dragging #result-gallery .thumbnail-item:hover {transform: none; box-shadow: none;}
#ms-band {
    position: absolute;
    z-index: 60;
    border: 1.5px solid rgba(167,139,250,.9);
    background: rgba(139,92,246,.13);
    border-radius: 6px;
    pointer-events: none;
    box-shadow: inset 0 0 18px rgba(139,92,246,.18);
}
#result-gallery .thumbnail-item.ms-picked {
    border-color: #c4b5fd !important;
    box-shadow: 0 0 0 2.5px rgba(167,139,250,.95), 0 8px 22px rgba(139,92,246,.4);
}
#result-gallery .thumbnail-item.ms-picked::after {
    content: '✓';
    position: absolute;
    top: 7px;
    right: 7px;
    width: 22px;
    height: 22px;
    border-radius: 999px;
    background: linear-gradient(135deg, #8b5cf6, #d946ef);
    color: #fff;
    font-size: 13px;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 8px rgba(0,0,0,.45);
    z-index: 5;
    pointer-events: none;
    animation: ms-pop .16s ease;
}
@keyframes ms-pop {from {opacity: 0; transform: scale(.5);} to {opacity: 1; transform: scale(1);}}
#ms-pill {
    position: absolute;
    top: 14px;
    right: 18px;
    z-index: 55;
    padding: 6px 13px;
    border-radius: 999px;
    font-size: .78rem;
    font-weight: 600;
    letter-spacing: .1px;
    background: linear-gradient(135deg, rgba(139,92,246,.94), rgba(217,70,239,.94));
    color: #fff;
    box-shadow: 0 6px 18px rgba(139,92,246,.45);
    opacity: 0;
    transform: translateY(-6px);
    pointer-events: none;
    transition: opacity .18s ease, transform .18s ease;
}
#ms-pill.on {opacity: 1; transform: translateY(0);}

/* ---------- custom image lightbox (Gradio's preview is disabled — we own it) ---------- */
#nb-lightbox {
    position: fixed;
    inset: 0;
    z-index: 950;
    display: flex;
    align-items: center;
    justify-content: center;
    background:
        radial-gradient(1100px 520px at 50% -80px, rgba(139,92,246,.16), transparent 70%),
        rgba(6,6,9,.94);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    animation: lb-in .16s ease;
}
@keyframes lb-in {from {opacity: 0;} to {opacity: 1;}}
#nb-lightbox img.lb-img,
#nb-lightbox video.lb-vid {
    /* pre-load fallback that can never crop. The moment the image loads,
       lbFit() computes the EXACT pixel size in JS (whole image, as big as the
       window allows) and pins it inline — no stylesheet can interfere. */
    flex: none;
    width: auto;
    height: auto;
    max-width: calc(100vw - 32px);
    max-height: calc(100vh - 136px);
    object-fit: contain;
    /* v4.9 zoom: gestures on the image belong to the app, not the browser */
    touch-action: none;
    will-change: transform;
    filter: drop-shadow(0 20px 60px rgba(0,0,0,.6));
    animation: lb-in .18s ease;
}
#nb-lightbox .lb-top {
    position: absolute;
    top: 14px;
    left: 18px;
    right: 18px;
    display: flex;
    align-items: center;
    gap: 10px;
    z-index: 5;
}
#nb-lightbox .lb-name {
    font-size: .78rem;
    font-weight: 600;
    color: #ddd;
    background: rgba(15,15,18,.72);
    padding: 7px 13px;
    border-radius: 999px;
    max-width: 46vw;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
}
#nb-lightbox .lb-actions {margin-left: auto; display: flex; gap: 8px;}
#nb-lightbox .lb-actions button {
    width: 36px;
    height: 36px;
    border-radius: 999px;
    border: none;
    background: rgba(15,15,18,.72);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    color: #eee;
    font-size: 15px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 12px rgba(0,0,0,.45);
}
#nb-lightbox .lb-actions button:hover {background: rgba(139,92,246,.9); color: #fff;}
#nb-lightbox .lb-actions .lb-del:hover {background: rgba(220,60,70,.92);}
#nb-lightbox .lb-nav {
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    width: 46px;
    height: 46px;
    border-radius: 999px;
    border: none;
    background: rgba(15,15,18,.72);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    color: #eee;
    font-size: 24px;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 2px 12px rgba(0,0,0,.45);
    z-index: 5;
}
#nb-lightbox .lb-nav:hover {background: rgba(139,92,246,.9); color: #fff;}
#nb-lightbox .lb-nav.prev {left: 16px;}
#nb-lightbox .lb-nav.next {right: 16px;}
#nb-lightbox .lb-strip {
    position: absolute;
    left: 50%;
    bottom: 12px;
    transform: translateX(-50%);
    display: flex;
    gap: 6px;
    max-width: calc(100vw - 44px);
    padding: 6px 10px;
    border-radius: 999px;
    background: rgba(15,15,18,.74);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    box-shadow: 0 8px 24px rgba(0,0,0,.45);
    overflow-x: auto;
    scrollbar-width: none;
    z-index: 5;
}
#nb-lightbox .lb-strip::-webkit-scrollbar {display: none;}
#nb-lightbox .lb-strip img,
#nb-lightbox .lb-strip video {
    width: 42px;
    height: 42px;
    flex: none;
    object-fit: cover;
    border-radius: 8px;
    opacity: .5;
    cursor: pointer;
    transition: opacity .12s ease, box-shadow .12s ease;
}
#nb-lightbox .lb-strip img:hover, #nb-lightbox .lb-strip video:hover {opacity: .85;}
#nb-lightbox .lb-strip img.current,
#nb-lightbox .lb-strip video.current {opacity: 1; box-shadow: 0 0 0 2px rgba(167,139,250,.95);}

/* ---------- composer ---------- */
#composer {
    position: sticky;
    bottom: 10px;
    z-index: 40;
    border-radius: 20px !important;
    padding: 10px 14px !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 14px 44px rgba(0,0,0,.5);
    border: 1px solid rgba(139,92,246,.24) !important;
    background: color-mix(in srgb, var(--block-background-fill) 76%, transparent) !important;
    backdrop-filter: blur(16px);
    -webkit-backdrop-filter: blur(16px);
}
#composer .form {border: none !important; background: transparent !important;}
/* ---------- paste box: clean dashed "drop tile" ---------- */
#paste-box {
    border-radius: 14px;
    overflow: hidden;
    border: 1.5px dashed rgba(139,92,246,.45) !important;
    background: rgba(139,92,246,.05) !important;
    transition: border-color .15s ease, background .15s ease, box-shadow .15s ease;
    --paste-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect width='8' height='4' x='8' y='2' rx='1'/%3E%3Cpath d='M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2'/%3E%3C/svg%3E");
}
#paste-box:hover {
    border-color: rgba(167,139,250,.95) !important;
    background: rgba(139,92,246,.13) !important;
    box-shadow: 0 6px 20px rgba(139,92,246,.28);
}
#paste-box button, #paste-box .upload-container {height: 100% !important;}
/* Gradio's upload area demands min-height 160px+ — kill it, center our own content */
#paste-box .wrap {
    display: flex !important;
    flex-direction: column !important;
    align-items: center !important;
    justify-content: center !important;
    height: 100% !important;
    min-height: 0 !important;
    padding: 0 !important;
    gap: 0 !important;
    font-size: 0 !important;   /* hides the stock caption text */
    background: transparent !important;
    overflow: hidden !important;
}
#paste-box .wrap svg, #paste-box .icon-wrap {display: none !important;}
#paste-box span, #paste-box p {display: none !important;}
#paste-box .wrap::before {
    content: '';
    width: 20px;
    height: 20px;
    margin-bottom: 6px;
    background: #c4b5fd;
    -webkit-mask: var(--paste-icon) center / contain no-repeat;
    mask: var(--paste-icon) center / contain no-repeat;
}
#paste-box .wrap::after {
    content: 'Paste image\A click, then Ctrl+V';
    white-space: pre;
    text-align: center;
    font-size: .64rem;
    line-height: 1.7;
    font-weight: 600;
    letter-spacing: .2px;
    color: rgba(233,228,255,.7);
}
#go-btn {
    font-size: 1.02rem !important;
    border-radius: 14px !important;
    background: linear-gradient(135deg, #8b5cf6, #d946ef) !important;
    border: none !important;
    box-shadow: 0 4px 18px rgba(139,92,246,.45) !important;
}
#go-btn:hover {transform: translateY(-1px); box-shadow: 0 8px 26px rgba(139,92,246,.6) !important;}
#go-btn:active {transform: translateY(0) scale(.98);}
#mode-hint {font-size: .72rem; opacity: .5; margin: 0 0 0 4px;}
/* v6.0: pre-flight cost — what this run will cost BEFORE you hit Generate */
#cost-estimate {
    margin: 8px 4px 2px 4px;
    padding: 8px 12px;
    border-radius: 12px;
    border: 1px solid rgba(139,92,246,.32);
    background: rgba(139,92,246,.10);
    font-size: .82rem;
    color: #efeaff;
}
#cost-estimate b {font-size: .95rem;}
#cost-estimate .ce-unit {opacity: .8;}
#cost-estimate .ce-left {opacity: .6;}
/* the same number rides in the composer pill so you see it without opening settings.
   The PRICE must never be the thing that gets clipped, so the model name is the part
   allowed to ellipsis away when the pill is narrow (phones). */
#model-pill .mp-cost {opacity: .9; font-weight: 700; flex: 0 0 auto; white-space: nowrap;}
#model-pill .mp-text {
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
#model-pill .mp-caret {flex: 0 0 auto;}
/* aspect-ratio preview: fixed-size slot so changing ratios never reflows the row */
#ratio-box {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 100%;
    height: 46px;
}
#ratio-box > div {animation: ratio-pop .18s ease;}
@keyframes ratio-pop {from {opacity: 0; transform: scale(.82);} to {opacity: 1; transform: scale(1);}}

/* Gradio bug: a closed dropdown can leave its option list stuck on screen as a
   "ghost" menu (state says closed, node keeps rendering). If the box reports
   closed, never draw the list. */
.wrap:has(input[aria-expanded="false"]) > ul.options {display: none !important;}

/* dropdown option lists: frosted card, never clipped by the composer */
ul.options {
    border-radius: 12px !important;
    border: 1px solid rgba(139,92,246,.3) !important;
    background: color-mix(in srgb, var(--block-background-fill) 94%, transparent) !important;
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    box-shadow: 0 14px 44px rgba(0,0,0,.55) !important;
    z-index: 200 !important;
}
ul.options li.item:hover, ul.options li.item.active {background: rgba(139,92,246,.2) !important;}

/* Composer sits at the screen bottom, so its menus must open UPWARD or the
   viewport cuts them off. (Also: backdrop-filter hijacks fixed positioning,
   so anchor the list to its own dropdown box.) Beats Gradio's inline styles. */
#composer .wrap {position: relative;}
#composer ul.options {
    position: absolute !important;
    top: auto !important;
    bottom: calc(100% + 6px) !important;
    left: 0 !important;
    min-width: 100% !important;
    width: max-content !important;
    max-height: 42vh !important;
}
/* top-of-page dropdowns just get a sane height cap */
#projectbar ul.options {max-height: 45vh !important;}

/* Gradio 6.20 wraps components in containers with overflow:hidden — an open
   menu gets cut off at the container edge (only ~1 option visible). While a
   menu is open, let its whole ancestor chain overflow. JS un-clips too as a
   belt-and-braces fallback (see fixOpenMenus). */
:is(#composer, #canvas-card, #projectbar):has(ul.options),
:is(#composer, #canvas-card, #projectbar) :is(.form, .row, .block, .container, .wrap, .styler):has(ul.options) {
    overflow: visible !important;
}

/* hidden bridge input that carries the multi-selection to Python */
.ghost-input {
    position: absolute !important;
    width: 1px !important;
    height: 1px !important;
    min-width: 0 !important;
    padding: 0 !important;
    margin: 0 !important;
    border: none !important;
    opacity: 0 !important;
    pointer-events: none !important;
    overflow: hidden !important;
    z-index: -1;
}

/* ---------- setup modal ---------- */
#setup-wrap {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    z-index: 500;
    width: min(720px, 94vw);
    height: min(85vh, 950px);
}
#setup-modal {
    border-radius: 18px !important;
    padding: 10px 22px 18px 22px !important;
    box-shadow: 0 24px 80px rgba(0,0,0,.6);
    border: 1px solid rgba(139,92,246,.35) !important;
    display: flex;
    flex-direction: column;
    height: 100%;
    background: color-mix(in srgb, var(--block-background-fill) 92%, transparent) !important;
    backdrop-filter: blur(10px);
}
#setup-guide-body {
    flex: 1 1 auto !important;
    min-height: 0 !important;
    overflow-y: auto !important;
    font-size: .95rem;
    line-height: 1.6;
    padding-right: 6px;
}
#setup-modal code {user-select: all;}

/* selected-image caption (shows the prompt that made it) */
#sel-caption {
    font-size: .75rem;
    opacity: .65;
    display: inline-block;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

/* ---------- live progress cards under the canvas ---------- */
.status-head {font-size: .85rem;}
.run-card {
    margin-top: 6px;
    padding: 8px 12px;
    border-radius: 12px;
    border: 1px solid rgba(139,92,246,.25);
    background: rgba(139,92,246,.07);
}
.run-top {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    font-size: .78rem;
    margin-bottom: 6px;
    opacity: .92;
}
.run-err {color: #fca5a5;}
.run-bar {height: 8px; border-radius: 999px; background: rgba(139,92,246,.15); overflow: hidden;}
.run-bar i {
    display: block;
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #8b5cf6, #d946ef, #8b5cf6);
    background-size: 200% 100%;
    animation: run-flow 1.4s linear infinite;
    transition: width .6s ease;
}
@keyframes run-flow {to {background-position: -200% 0;}}
.run-bar.indet i {width: 30% !important; animation: run-indet 1.1s ease-in-out infinite alternate;}
@keyframes run-indet {from {margin-left: 0;} to {margin-left: 70%;}}
.run-done {font-size: .78rem; opacity: .8; margin-top: 5px;}
.run-done.warn {color: #fcd34d; opacity: .95;}

/* ---------- ingredient tray: neat square tiles ---------- */
#ingredient-tray .grid-wrap {padding: 6px !important;}
#ingredient-tray .grid-container {
    grid-template-rows: none !important;
    grid-template-columns: repeat(auto-fill, 88px) !important;
    grid-auto-rows: 88px !important;
    gap: 8px !important;
    justify-content: start;
}
#ingredient-tray .gallery-item {width: 88px !important; height: 88px !important;}
#ingredient-tray .thumbnail-item {
    position: relative;
    overflow: hidden;
    width: 100% !important;
    height: 100% !important;
    aspect-ratio: 1 / 1 !important;
    border-radius: 12px !important;
    border: none !important;
    background: #131316 !important;
}
#ingredient-tray .thumbnail-item img {width: 100%; height: 100%; object-fit: cover !important;}
#ingredient-tray .caption-label {
    left: 4px !important;
    right: auto !important;
    bottom: 4px !important;
    border: none !important;
    border-radius: 7px !important;
    background: rgba(15,15,18,.8) !important;
    color: #e9e4ff !important;
    padding: 1px 7px !important;
    font-size: .66rem !important;
    font-weight: 700 !important;
}
#ingredient-tray .thumb-actions {top: 4px; right: 4px; gap: 4px;}
#ingredient-tray .thumb-actions button {width: 24px; height: 24px; font-size: 12px;}

/* ---------- proper icons on buttons (mask SVGs inherit the button's text color) ---------- */
.trash-btn::before, .eraser-btn::before, .ico-help::before, .ico-plus::before,
.ico-folder::before, .ico-refresh::before, .ico-imgplus::before, .ico-save::before,
.ico-plug::before {
    content: '';
    width: 13px;
    height: 13px;
    flex: none;
    display: inline-block;
    vertical-align: -2px;
    margin-right: 7px;
    background: currentColor;
    -webkit-mask: var(--btn-icon) center / contain no-repeat;
    mask: var(--btn-icon) center / contain no-repeat;
}
.trash-btn {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 6h18'/%3E%3Cpath d='M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6'/%3E%3Cpath d='M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2'/%3E%3Cpath d='M10 11v6'/%3E%3Cpath d='M14 11v6'/%3E%3C/svg%3E");}
.eraser-btn {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='m7 21-4.3-4.3c-1-1-1-2.5 0-3.4l9.6-9.6c1-1 2.5-1 3.4 0l5.6 5.6c1 1 1 2.5 0 3.4L13 21'/%3E%3Cpath d='M22 21H7'/%3E%3Cpath d='m5 11 9 9'/%3E%3C/svg%3E");}
.ico-help {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='10'/%3E%3Cpath d='M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3'/%3E%3Cpath d='M12 17h.01'/%3E%3C/svg%3E");}
.ico-plus {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.6' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M5 12h14'/%3E%3Cpath d='M12 5v14'/%3E%3C/svg%3E");}
.ico-folder {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z'/%3E%3C/svg%3E");}
.ico-refresh {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8'/%3E%3Cpath d='M21 3v5h-5'/%3E%3Cpath d='M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16'/%3E%3Cpath d='M8 16H3v5'/%3E%3C/svg%3E");}
.ico-imgplus {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M16 5h6'/%3E%3Cpath d='M19 2v6'/%3E%3Cpath d='M21 11.5V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7.5'/%3E%3Cpath d='m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21'/%3E%3Ccircle cx='9' cy='9' r='2'/%3E%3C/svg%3E");}
.ico-save {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M15.2 3a2 2 0 0 1 1.4.6l3.8 3.8a2 2 0 0 1 .6 1.4V19a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z'/%3E%3Cpath d='M17 21v-7a1 1 0 0 0-1-1H8a1 1 0 0 0-1 1v7'/%3E%3Cpath d='M7 3v4a1 1 0 0 0 1 1h7'/%3E%3C/svg%3E");}
.ico-plug {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M6.3 20.3a2.4 2.4 0 0 0 3.4 0L12 18l-6-6-2.3 2.3a2.4 2.4 0 0 0 0 3.4Z'/%3E%3Cpath d='m2 22 3-3'/%3E%3Cpath d='M7.5 13.5 10 11'/%3E%3Cpath d='M10.5 16.5 13 14'/%3E%3Cpath d='m18 3-4 4h6l-4 4'/%3E%3C/svg%3E");}

/* ---------- empty canvas hint ---------- */
#empty-hint {
    position: absolute;
    inset: 0 0 60px 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 7px;
    text-align: center;
    pointer-events: none;
    z-index: 5;
    font-family: 'Outfit', Inter, sans-serif;
    font-size: 1.2rem;
    font-weight: 700;
    letter-spacing: -.2px;
    color: rgba(196,181,253,.85);
}
#empty-hint span {
    font-family: Inter, ui-sans-serif, sans-serif;
    font-size: .8rem;
    font-weight: 500;
    letter-spacing: 0;
    opacity: .55;
    color: var(--body-text-color);
    max-width: 420px;
}

/* prompt box: comfy but Flow-compact text */
#prompt-box textarea {font-size: .9rem !important; line-height: 1.5 !important;}

/* v4.9 iOS: long-press must NEVER open the system save/copy/look-up sheet —
   our long-press shows the app's own actions instead */
#result-gallery img, #result-gallery video,
#ingredient-tray img, #nb-lightbox img, #nb-lightbox video {
    -webkit-touch-callout: none !important;
    -webkit-user-select: none !important;
    user-select: none !important;
    -webkit-user-drag: none;
}

/* v5.1 mobile: a long-press on chrome (e.g. holding the ＋ paste button) must not
   start a text selection that sweeps the whole page. Disable selection on the app
   shell, then re-enable it for the real text fields so typing still works.
   NOTE: do NOT write `.gradio-container` here — Gradio's CSS scoper prepends its own
   `.gradio-container … .contain ` prefix, so a literal `.gradio-container` becomes a
   never-matching DOUBLE-nested selector. Bare `*` / element selectors get scoped
   correctly to `.contain *`, which is exactly the app shell we want. */
@media (max-width: 760px) {
    * {
        -webkit-user-select: none !important;
        user-select: none !important;
        -webkit-touch-callout: none !important;
    }
    input, textarea, [contenteditable] {
        -webkit-user-select: text !important;
        user-select: text !important;
    }
}

/* v4.9: unmissable ▶ badge on video tiles (paint() adds .vid-tile) */
#result-gallery .thumbnail-item.vid-tile::before {
    content: '▶';
    position: absolute;
    top: 8px;
    left: 8px;
    width: 30px;
    height: 30px;
    border-radius: 999px;
    background: rgba(0,0,0,.68);
    border: 1px solid rgba(255,255,255,.35);
    color: #fff;
    font-size: 12px;
    line-height: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    padding-left: 2px;              /* optical centering of the triangle */
    z-index: 24;                    /* under .thumb-actions (25) */
    pointer-events: none;
    box-shadow: 0 2px 10px rgba(0,0,0,.55);
    /* v6.0 PERF: no backdrop-filter here — this badge repeats on EVERY video tile
       and each blur layer costs GPU time. The solid fill above looks the same. */
}

/* v6.0: Save / Test-connection result — used to be an unstyled Markdown that was
   effectively invisible against the modal. Now a proper status panel. */
/* Selector needs TWO ids: the older chrome rule
   `:is(#settings-modal-body, …) :is(.block, .form) {background:transparent !important;
   border:none !important}` also matches this element and, at equal specificity, would
   win on source order. #settings-modal-body #key-status outranks it. */
#settings-modal-body #key-status {
    margin: 8px 0 2px 0;
    padding: 10px 14px;
    border-radius: 12px;
    border: 1px solid rgba(255,255,255,.14) !important;
    border-left: 4px solid #a78bfa !important;
    background: rgba(255,255,255,.07) !important;
}
#key-status p, #key-status li {
    font-size: .86rem !important;
    line-height: 1.5 !important;
    margin: 0 0 4px 0 !important;
    color: #f2f0ff !important;
    word-break: break-word;
}
#key-status code {font-size: .8rem; user-select: all;}
/* tint by outcome — the text already starts with ✅ / ⚠️ / ❌ */
#settings-modal-body #key-status.ok   {border-left-color: #4ade80 !important; background: rgba(74,222,128,.13) !important;}
#settings-modal-body #key-status.warn {border-left-color: #fbbf24 !important; background: rgba(251,191,36,.13) !important;}
#settings-modal-body #key-status.bad  {border-left-color: #f87171 !important; background: rgba(248,113,113,.13) !important;}
#settings-modal-body #key-status.blank {display: none !important;}

/* v4.9: small helper text + trash gallery inside the Settings modal */
.hint-text {font-size: .75rem !important; opacity: .6;}
/* v6.0: the trash reads as a real panel now (it was an invisible dark strip) */
#trash-head {margin-top: 6px;}
#trash-panel {
    border: 1px solid rgba(255,255,255,.12) !important;
    border-radius: 14px !important;
    padding: 10px 10px 12px 10px !important;
    background: rgba(255,255,255,.035) !important;
    margin-bottom: 4px;
    /* Gradio Groups are overflow:hidden, which was clipping the action-button row */
    overflow: visible !important;
}
#trash-panel .row {overflow: visible !important;}
#trash-panel button {min-height: 32px !important;}
#trash-gallery {min-height: 120px;}
/* fixed 6-up thumbnails — Gradio collapses the grid to the item count (1 item
   → one GIANT stretched column), same landmine as the main gallery */
#trash-gallery .grid-container {
    grid-template-columns: repeat(6, minmax(0, 1fr)) !important;
    grid-auto-rows: 84px !important;
    gap: 6px !important;
}
#trash-gallery .thumbnail-item {
    width: 100% !important;
    height: 84px !important;
    border-radius: 10px !important;
    overflow: hidden;
}
#trash-gallery .thumbnail-item img, #trash-gallery .thumbnail-item video {
    width: 100%;
    height: 100%;
    object-fit: cover;
}
#trash-gallery .grid-wrap {max-height: 200px; overflow-y: auto;}

/* v6.0: the paste sheet — the dependable mobile paste path (native Paste menu) */
#paste-sheet {
    position: fixed;
    inset: 0;
    z-index: 900;
    background: rgba(0,0,0,.55);
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 18px;
}
#paste-sheet .ps-card {
    width: min(380px, 94vw);
    background: rgba(24,24,28,.99);
    border: 1px solid rgba(255,255,255,.14);
    border-radius: 18px;
    padding: 16px;
    box-shadow: 0 24px 80px rgba(0,0,0,.7);
}
#paste-sheet .ps-title {font-size: .95rem; font-weight: 700; margin-bottom: 10px;}
#paste-sheet .ps-input {
    width: 100%;
    font-size: 16px;                      /* 16px = no iOS auto-zoom */
    padding: 12px 12px;
    border-radius: 12px;
    border: 1px solid rgba(167,139,250,.55);
    background: rgba(255,255,255,.06);
    color: #eee;
    -webkit-user-select: text !important;
    user-select: text !important;
}
#paste-sheet .ps-hint {font-size: .76rem; opacity: .7; margin: 10px 2px 12px 2px;}
#paste-sheet .ps-cancel {
    width: 100%;
    padding: 10px;
    border-radius: 12px;
    border: 1px solid rgba(255,255,255,.14);
    background: rgba(255,255,255,.05);
    color: #eee;
    font-weight: 600;
    cursor: pointer;
}
/* v6.0: ✕ to dismiss the settings sheet on a phone (sticky so it survives scroll) */
#settings-pop-close {
    position: sticky;
    top: 0;
    float: right;
    z-index: 5;
    width: 32px; height: 32px;
    margin: -4px -4px 0 0;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,.16);
    background: rgba(30,30,36,.96);
    color: #eee;
    font-size: 14px;
    line-height: 1;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer;
}

/* 📋 paste affordance inside the Start/End frame slots (touch only) */
.frame-paste-btn {
    position: absolute;
    right: 6px;
    bottom: 6px;
    z-index: 26;
    width: 30px; height: 30px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,.18);
    background: rgba(15,15,18,.82);
    color: #eee;
    font-size: 13px;
    line-height: 1;
    display: flex; align-items: center; justify-content: center;
    cursor: pointer;
}
#frame-start, #frame-end {position: relative;}

/* v4.9 touch: the "Select" chip (multi-select entry point on phones) */
#sel-chip {
    position: absolute;
    top: 14px;
    left: 18px;
    z-index: 54;
    padding: 6px 13px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,.16);
    background: rgba(15,15,18,.72);
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    color: #eee;
    font-size: .78rem;
    font-weight: 600;
    cursor: pointer;
    display: none;                       /* JS shows it on touch devices only */
}
#sel-chip.on {
    background: linear-gradient(135deg, rgba(139,92,246,.94), rgba(217,70,239,.94));
    border-color: transparent;
    color: #fff;
}

/* touch selection pill: carries tappable actions on phones */
#ms-pill.touchy {pointer-events: auto;}
#ms-pill .pill-act {
    margin-left: 8px;
    padding: 4px 12px;
    border-radius: 999px;
    border: none;
    background: rgba(255,255,255,.25);
    color: #fff;
    font-size: .74rem;
    font-weight: 700;
    cursor: pointer;
}
#ms-pill .pill-del {background: rgba(15,15,18,.55);}
/* tray action cluster shown by tap on touch devices (no hover on phones) */
.thumb-actions.show {opacity: 1 !important; pointer-events: auto !important;}
/* v4.0: the + (make ingredient) action never shows on video thumbnails */
.thumb-actions.is-vid .act-add {display: none !important;}

/* ---------- v4.0: Image/Video toggle + video mode as violet pill segments ---------- */
#media-toggle, #vmode-radio {background: transparent; border: none; padding: 0;}
#media-toggle .wrap, #vmode-radio .wrap {gap: 6px; flex-wrap: nowrap;}
#media-toggle label, #vmode-radio label {
    background: rgba(255,255,255,.05);
    border: 1px solid rgba(139,92,246,.22);
    border-radius: 999px;
    padding: 7px 12px;
    margin: 0;
    cursor: pointer;
    user-select: none;
    transition: background .15s, border-color .15s;
}
#media-toggle label:hover, #vmode-radio label:hover {border-color: rgba(167,139,250,.55);}
#media-toggle label.selected, #vmode-radio label.selected,
#media-toggle label:has(input:checked), #vmode-radio label:has(input:checked) {
    background: rgba(139,92,246,.28);
    border-color: rgba(167,139,250,.8);
}
#media-toggle input, #vmode-radio input {
    position: absolute; opacity: 0; width: 0; height: 0; pointer-events: none;
}
#media-toggle span, #vmode-radio span {font-size: .82rem; font-weight: 600;}

/* v4.0: Frames-mode row — start/end frame slots */
#video-row {align-items: flex-end; row-gap: 6px;}
#frame-start, #frame-end {max-width: 176px;}
#frame-start .wrap, #frame-end .wrap {min-height: 0;}
/* the greyed-out end slot on Omni Flash reads as off, not broken */
#frame-end:has(.block[aria-disabled]), #frame-end .block:not(:has(button)) {opacity: .55;}

/* touch niceties everywhere: kill the double-tap zoom on controls (v5.0 broadened
   to the JS-built non-button elements + the whole composer/top bar, so fast taps
   never accidentally zoom the page — the #model-pill is a <div>, gaps between the
   pill's buttons and the settings-sheet controls weren't covered before) */
button, .thumbnail-item, #media-toggle label, #vmode-radio label,
#composer, #composer *, #pill-row, #model-pill, #sel-chip, #ms-pill,
.thumb-actions, .thumb-actions *, #settings-pop, #settings-pop *,
#topbar, #topbar * {touch-action: manipulation;}

/* ============================================================
   FLOW REDESIGN — near-black surfaces + grey pill controls, a
   centered composer pill with a pop-up settings panel, and a
   floating top-right View panel. Violet stays the ACCENT only
   (Generate button, focus, hover, selection). Sits before the
   mobile @media block so phone overrides still win.
   ============================================================ */

/* ---- neutral, Flow-dark chrome surfaces ---- */
#projectbar {
    border: 1px solid rgba(255,255,255,.08) !important;
    background: rgba(255,255,255,.028) !important;
}
#canvas-card {
    border: 1px solid rgba(255,255,255,.07) !important;
    background: rgba(255,255,255,.014) !important;
}
/* neutral action buttons become soft grey pills (destructive/stop stay red) */
:is(.ico-help, .ico-plus, .ico-folder, .ico-refresh, .ico-save, .ico-plug,
    .eraser-btn, #reset-btn) {
    border-radius: 999px !important;
    background: rgba(255,255,255,.05) !important;
    border: 1px solid rgba(255,255,255,.10) !important;
}
:is(.ico-help, .ico-plus, .ico-folder, .ico-refresh, .ico-save, .ico-plug,
    .eraser-btn, #reset-btn):hover {
    background: rgba(139,92,246,.24) !important;
    border-color: rgba(167,139,250,.55) !important;
}

/* ---- composer: a centered Flow-style pill (v4.7: Flow-sized, ~760px) ---- */
#composer {
    max-width: 760px;
    margin: 0 auto !important;
    left: 0 !important;
    right: 0 !important;
    border-radius: 26px !important;
    padding: 5px 6px !important;
    overflow: visible !important;
    border: 1px solid rgba(255,255,255,.11) !important;
    background: rgba(23,23,27,.92) !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 12px 44px rgba(0,0,0,.55) !important;
}
#pill-row {
    display: flex !important;
    flex-wrap: nowrap !important;
    align-items: flex-end;
    gap: 6px !important;
    background: transparent !important;
}
/* the prompt reads as a borderless field inside the pill */
#composer #prompt-box {flex: 1 1 auto !important; min-width: 0 !important;}
#composer #prompt-box .wrap,
#composer #prompt-box .container,
#composer #prompt-box .block {background: transparent !important; border: none !important;}
#composer #prompt-box textarea {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    resize: none !important;
    padding: 8px 8px !important;
    font-size: .9rem !important;
}
/* hide the old dashed paste tile — Ctrl+V / drag & drop work page-wide */
#composer #paste-box {display: none !important;}

/* circular ＋ (add images) on the left of the pill */
#composer #add-files-btn, #composer #add-files-btn button {
    min-width: 38px !important;
    width: 38px !important;
    height: 38px !important;
    border-radius: 999px !important;
    padding: 0 !important;
    flex: none !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
    font-size: 0 !important;                 /* hides the "Add images" text */
    background: rgba(255,255,255,.06) !important;
    border: 1px solid rgba(255,255,255,.11) !important;
    box-shadow: none !important;
}
#composer #add-files-btn.ico-imgplus::before,
#composer #add-files-btn .ico-imgplus::before {
    margin-right: 0 !important;
    width: 17px !important;
    height: 17px !important;
    vertical-align: middle !important;
}
#composer #add-files-btn:hover, #composer #add-files-btn:hover button {
    background: rgba(139,92,246,.28) !important;
    border-color: rgba(167,139,250,.6) !important;
}
/* circular → submit on the right (keeps its violet gradient) */
#composer #go-btn {
    min-width: 40px !important;
    width: 40px !important;
    height: 40px !important;
    border-radius: 999px !important;
    padding: 0 !important;
    flex: none !important;
    font-size: 1.2rem !important;
    line-height: 1 !important;
    display: flex !important;
    align-items: center !important;
    justify-content: center !important;
}

/* JS-built model pill inside the composer — shows the current model + Outputs,
   and opens the settings pop-up */
#model-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex: none;
    height: 34px;
    padding: 0 12px;
    border-radius: 999px;
    background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.12);
    color: #e9e4ff;
    font-size: .75rem;
    font-weight: 600;
    cursor: pointer;
    white-space: nowrap;
    max-width: 230px;
    overflow: hidden;
    text-overflow: ellipsis;
    transition: background .15s, border-color .15s;
}
#model-pill:hover, #composer.pop-open #model-pill {
    background: rgba(139,92,246,.26);
    border-color: rgba(167,139,250,.7);
}
#model-pill .mp-caret {opacity: .55; font-size: .68rem; margin-left: 1px;}

/* ---- pop-up settings panel: floats ABOVE the pill ---- */
#settings-pop {
    position: absolute !important;
    left: 6px;
    right: 6px;
    bottom: calc(100% + 12px);
    z-index: 60;
    padding: 14px 16px !important;
    border-radius: 22px !important;
    background: rgba(22,22,26,.98) !important;
    border: 1px solid rgba(255,255,255,.12) !important;
    box-shadow: 0 22px 66px rgba(0,0,0,.62) !important;
    overflow: visible !important;
    opacity: 0;
    visibility: hidden;
    transform: translateY(6px);
    pointer-events: none;
}
#composer.pop-open #settings-pop {
    opacity: 1;
    visibility: visible;
    transform: translateY(0);
    pointer-events: auto;
}
#settings-row {flex-wrap: wrap !important; gap: 8px !important; align-items: flex-end;}

/* ---- View panel toggle: a normal top-bar icon button (lives beside Settings,
   so it never covers the grid or an image's hover actions) ---- */
#view-toggle {
    width: 34px !important;
    min-width: 34px !important;   /* Gradio's base button CSS forces min-width:160px */
    max-width: 34px !important;
    height: 34px;
    flex: none;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,.12);
    background: rgba(255,255,255,.05);
    color: #e9e4ff;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    transition: background .15s, border-color .15s;
}
#view-toggle:hover, #canvas-card.view-open #view-toggle {
    background: rgba(139,92,246,.28);
    border-color: rgba(167,139,250,.7);
}
#view-toggle svg {width: 16px; height: 16px;}
#view-panel {
    position: absolute !important;
    top: 60px;
    right: 14px;
    z-index: 64;
    width: 216px;
    padding: 12px 14px !important;
    border-radius: 16px !important;
    background: rgba(22,22,26,.98) !important;
    border: 1px solid rgba(255,255,255,.12) !important;
    box-shadow: 0 18px 50px rgba(0,0,0,.58) !important;
    display: flex;
    flex-direction: column;
    gap: 9px;
    overflow: visible !important;
    opacity: 0;
    visibility: hidden;
    pointer-events: none;
}
#canvas-card.view-open #view-panel {
    opacity: 1;
    visibility: visible;
    pointer-events: auto;
}
#view-title, #view-title p {
    font-size: .7rem !important;
    letter-spacing: .5px;
    text-transform: uppercase;
    opacity: .5;
    font-weight: 700;
    margin: 0 !important;
}

/* ---- canvas footer (status + delete) reads quiet, Flow-style ---- */
#canvas-footer {align-items: center; gap: 10px !important; margin-top: 6px;}

/* ============================================================
   FLOW REFINEMENT PASS (v4.2) — real Flow minimalism: quiet
   labels, full-bleed grid, settings-as-modal, slim top bar.
   ============================================================ */

/* Gradio 6.20 fills block-info labels with solid violet — make them quiet text */
span[data-testid="block-info"] {
    background: transparent !important;
    color: rgba(233,228,255,.5) !important;
    padding: 0 0 5px 2px !important;
    border-radius: 0 !important;
    font-size: .72rem !important;
    font-weight: 600 !important;
    letter-spacing: .3px;
}

/* form controls read as clean dark Flow pills (strip Gradio's grey block box,
   round the input itself) */
:is(#settings-modal-body, #settings-row, #view-panel, #topbar) :is(.block, .form) {
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
}
:is(#settings-modal-body, #settings-row, #view-panel, #topbar) .wrap:has(input),
:is(#settings-modal-body, #settings-row, #topbar) :is(input[type="text"], input[type="number"], textarea),
#project-dd .wrap {
    background: rgba(255,255,255,.055) !important;
    border: 1px solid rgba(255,255,255,.10) !important;
    border-radius: 12px !important;
}
:is(#settings-modal-body, #settings-row, #view-panel, #topbar) .wrap:has(input:focus),
:is(#settings-modal-body, #settings-row, #topbar) :is(input, textarea):focus {
    border-color: rgba(167,139,250,.7) !important;
}
/* the value text / arrow sit inside the pill, no inner box */
:is(#settings-modal-body, #settings-row, #view-panel, #topbar, #project-dd)
    :is(.wrap-inner, .secondary-wrap) {background: transparent !important; border: none !important;}

/* ---- Flow full-bleed canvas: kill the card + the gallery's white frame ---- */
#canvas-card {
    border: none !important;
    background: transparent !important;
    padding: 0 !important;
}
#result-gallery, #result-gallery.block {
    border: none !important;
    background: transparent !important;
    box-shadow: none !important;
    overflow: visible !important;
}
#result-gallery .grid-wrap {
    max-height: calc(100vh - 168px) !important;
    padding-bottom: 92px;   /* last row clears the floating composer */
}
/* Gradio collapses the grid to the item count (1 image → 1 giant column);
   force the chosen column count so every tile stays a normal size */
#result-gallery .grid-container {
    grid-template-columns: repeat(var(--bs-cols, 4), minmax(0, 1fr)) !important;
    padding: 2px;
}

/* ---- slim, Flow-minimal top bar ---- */
#topbar {
    gap: 10px !important;
    padding: 0 2px 4px 2px;
    align-items: center;
    flex-wrap: nowrap;
}
h1.app-title {font-size: 1.05rem;}
#project-dd {min-width: 160px;}
#project-dd .wrap {border-radius: 999px !important; padding: 1px 6px !important;}
#topstats-wrap {margin-left: auto !important;}   /* push spend + Guide + View + Settings right */
#topstats {white-space: nowrap; padding-right: 6px; font-size: .74rem;}
#topbar button {min-height: 30px !important;}
/* the JS-built View button is a plain <button> — keep it a small icon pill, never
   let the flex row stretch it wide */
#view-toggle {flex: 0 0 34px !important;}
.ico-gear::before {
    content: '';
    width: 13px; height: 13px; flex: none;
    display: inline-block; vertical-align: -2px; margin-right: 7px;
    background: currentColor;
    -webkit-mask: var(--btn-icon) center / contain no-repeat;
    mask: var(--btn-icon) center / contain no-repeat;
}
.ico-gear {--btn-icon: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'%3E%3Ccircle cx='12' cy='12' r='3'/%3E%3Cpath d='M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z'/%3E%3C/svg%3E");}

/* ---- settings & projects modal (mirrors the setup modal) ---- */
#settings-wrap {
    position: fixed;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    z-index: 500;
    width: min(680px, 94vw);
    height: min(86vh, 920px);
}
#settings-modal {
    border-radius: 18px !important;
    padding: 8px 22px 18px 22px !important;
    box-shadow: 0 24px 80px rgba(0,0,0,.62);
    border: 1px solid rgba(255,255,255,.12) !important;
    display: flex; flex-direction: column; height: 100%;
    background: color-mix(in srgb, var(--block-background-fill) 94%, transparent) !important;
    backdrop-filter: blur(12px);
}
#settings-modal-head {align-items: center;}
#settings-modal-body {
    flex: 1 1 auto !important;
    min-height: 0 !important;
    overflow-y: auto !important;
    padding-right: 8px;
}
/* v6.0 ROOT FIX: #settings-modal-body is a flex COLUMN, so its children were being
   shrunk to fit — the section headings collapsed to ~5px and clipped their own text
   (overflow:auto), which is why "📁 Projects" / "🗑 Trash" / "💳 Spend tracker" were
   invisible. Children must keep their natural height. */
#settings-modal-body > * {flex: 0 0 auto !important;}
#settings-modal-body > .block {overflow: visible !important;}
/* v6.0: section headings were washed out (13px @ .8 opacity on grey) — the Trash
   one in particular needs to be findable at a glance */
#settings-modal-body h4 {
    margin: 16px 0 4px 0;
    opacity: 1;
    font-size: .92rem;
    font-weight: 700;
    letter-spacing: .2px;
    color: #d9d2ff !important;
}

/* ---- destructive confirm dialogs float dead-center over everything ----
   v6.4 — THE multi-delete bug. These used to be a single gr.Group carrying
   elem_classes=["confirm-card"], and Gradio 6.20 puts elem_classes on TWO
   nested divs. Both therefore became `position: fixed`, so the outer one had
   no in-flow content left and collapsed to an 18px strip — and its
   `overflow-y: auto` then CLIPPED AWAY the button row sitting below that
   18px. The dialog was in the DOM and "visible", but elementFromPoint over
   "Yes, delete them" returned the gallery: the buttons were unreachable, so
   deleting 2+ items looked like it did nothing at all.
   The fix is the shape #setup-wrap / #settings-wrap already used and that has
   always worked: an id'd wrapper that ONLY positions (ids can't be duplicated
   onto both divs), and an id'd box inside that ONLY decorates. Never put
   positioning on a Gradio elem_class. */
#img-confirm-wrap, #proj-confirm-wrap {
    position: fixed;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    /* above the settings modal (500) — "Delete project" is opened from inside
       it — and above the mobile settings sheet (80) */
    z-index: 950;
    width: min(440px, 92vw);
}
#img-confirm-box, #proj-confirm-box {
    border: 1px solid rgba(248,113,113,.5) !important;
    border-radius: 14px !important;
    padding: 10px 16px !important;
    max-height: 80vh;
    overflow-y: auto;
    background: rgba(26,20,24,.985) !important;
    box-shadow: 0 24px 80px rgba(0,0,0,.65) !important;
}
/* tap-sized buttons + a readable message on a phone */
@media (max-width: 760px) {
    #img-confirm-box, #proj-confirm-box {padding: 14px 16px !important;}
    #img-confirm-box .row, #proj-confirm-box .row {
        flex-wrap: wrap !important; gap: 8px !important;
    }
    #img-confirm-box button, #proj-confirm-box button {
        min-height: 44px !important; flex: 1 1 45% !important;
    }
    #img-confirm-box p, #img-confirm-box strong,
    #proj-confirm-box p, #proj-confirm-box strong {font-size: .9rem !important;}
}

/* ---- kill Gradio's grey ".styler" wrapper in the canvas & composer ---- */
#canvas-card .styler {background: transparent !important;}

/* composer: the outer group is just a transparent centered container — the
   visible PILL is #pill-row itself (fixes the grey double-box + narrow width
   that was clipping the prompt text) */
#composer {
    display: block !important;   /* let the inner styler fill the full 880px */
    background: transparent !important;
    border: none !important;
    box-shadow: none !important;
    padding: 0 !important;
}
#composer .styler {
    background: transparent !important;
    width: 100% !important;
    min-width: 0 !important;
    display: flex !important;
    flex-direction: column !important;
}
#composer #prompt-box .form {background: transparent !important; border: none !important;}
#pill-row {
    width: 100% !important;
    align-items: center !important;
    /* clean solid bar — no backdrop blur, so images around it stay crisp */
    background: rgba(20,20,24,.97) !important;
    border: 1px solid rgba(255,255,255,.12) !important;
    border-radius: 26px !important;
    padding: 5px 6px !important;
    box-shadow: inset 0 1px 0 rgba(255,255,255,.05), 0 6px 22px rgba(0,0,0,.45) !important;
}
/* nothing behind the prompt but the pill itself */
#composer #prompt-box, #composer #prompt-box * {background: transparent !important;}
#composer #prompt-box textarea {border: none !important; box-shadow: none !important;}

/* the aspect preview reads as clutter next to the Ratio dropdown — hide it
   (the Ratio dropdown already shows the shape + name) */
#settings-row #ratio-box, #settings-row .column:has(#ratio-box) {display: none !important;}

/* desktop: the page itself never scrolls — the grid is the only scroller
   (kills the stray 1px page scrollbar next to the grid's own) */
@media (min-width: 761px) {
    body {overflow-y: hidden;}
}

/* video Frames row (desktop): compact frame slots so the whole row —
   [Frames/Ingredients] [Start] [End] — sits on one clean line, no overlap.
   Scoped to desktop so the mobile 2-up rules below still win on phones. */
@media (min-width: 761px) {
    #video-row {flex-wrap: nowrap !important; align-items: flex-end !important; gap: 10px !important; margin-bottom: 4px;}
    #video-row #frame-start, #video-row #frame-end {
        flex: 0 0 150px !important;
        width: 150px !important;
        min-width: 150px !important;
        max-width: 150px !important;
    }
}
/* Gradio's floating media label ("Start frame" / "End frame") is a violet
   badge — darken it to match the Flow chrome */
[data-testid="block-label"] {
    background: rgba(15,15,18,.82) !important;
    color: rgba(233,228,255,.85) !important;
    border: none !important;
    padding: 3px 8px !important;
    font-size: .68rem !important;
}

/* ---- video Start/End frame slots: clean compact drop tiles ----
   (style the .wrap upload zone — it only exists while the slot is EMPTY, so an
   uploaded frame shows its image with no leftover dashed border. NO :has() — it
   mangles Gradio 6.20's CSS scoper.) */
#frame-start .wrap, #frame-end .wrap {
    border: 1.5px dashed rgba(139,92,246,.45) !important;
    border-radius: 12px !important;
    background: rgba(139,92,246,.05) !important;
    font-size: 0 !important;      /* hides "Drop Image Here / - or - / Click to Upload" */
    min-height: 0 !important;
    gap: 2px !important;
    padding: 8px !important;
}
#frame-start .wrap:hover, #frame-end .wrap:hover {
    border-color: rgba(167,139,250,.9) !important;
    background: rgba(139,92,246,.12) !important;
}
#frame-start .wrap svg, #frame-end .wrap svg {
    width: 22px !important;
    height: 22px !important;
    opacity: .55;
}
/* Ingredients video mode never uses Start/End frames — hide the slots outright
   (JS stamps .ingredients-mode on #video-row from the vmode radio, so this holds
   even if Gradio's vmode .change doesn't re-fire composer_visibility) */
#video-row.ingredients-mode #frame-start,
#video-row.ingredients-mode #frame-end {display: none !important;}

/* View panel now opens at the canvas top-right (its toggle lives in the top bar) */
#view-panel {top: 12px !important;}

/* ============================================================
   BOTTOM OVERLAY (v4.3) — the grid fills the whole screen and the
   composer + footer FLOAT over it, so images are visible all the
   way down behind/around the bar (the ~40% dead black zone is gone).
   ============================================================ */
/* gallery uses the full height and stays its own scroll container so the
   multi-select lasso keeps working */
#result-gallery {height: auto !important;}
#result-gallery .grid-wrap {
    /* v4.8: HEIGHT, not just max-height. Gradio stamps the Gallery height prop as an
       inline style on this very element; an inline `height: 58vh` was hard-clipping the
       image area at 58% of the window — the black rectangle below it. A stylesheet
       !important beats inline styles, so this pins the scroll container to the full
       viewport no matter what the prop says. */
    height: calc(100vh - 70px) !important;   /* wrap top sits at ~67px (below the top bar) */
    max-height: calc(100vh - 70px) !important;
    padding-top: 8px !important;
    padding-bottom: 96px !important;   /* just enough to scroll the last row clear of the bar */
}
/* composer floats (fixed) so it takes no layout height — images fill behind it;
   only the pill is opaque, everything around it is see-through */
#composer {
    position: fixed !important;
    bottom: 14px !important;
    left: 0 !important;
    right: 0 !important;
    z-index: 45 !important;
    backdrop-filter: none !important;   /* no frosted halo behind the bar */
    -webkit-backdrop-filter: none !important;
}
/* v4.7: the footer BAND is gone — the bottom of the screen shows ONLY the pill.
   #canvas-footer survives as an invisible bottom-left anchor where live run-cards
   (generation progress) stack upward; when idle it renders nothing at all. */
#canvas-footer {
    position: fixed !important;
    left: 16px !important;
    right: auto !important;
    bottom: 16px !important;
    z-index: 44 !important;
    width: min(330px, 34vw);
    max-width: none;
    margin: 0 !important;
    padding: 0 !important;
    display: block !important;
    background: transparent !important;
    border: none !important;
    pointer-events: none;              /* clicks pass through to the grid */
}
#canvas-footer > * {pointer-events: auto;}
/* "Delete selected" now lives in the selection pill — this stays in the DOM only
   as the hidden bridge the pill's JS clicks (same ghost-button trick as the rest) */
#del-img-btn {display: none !important;}
/* single-click caption: dropped from the UI — the lightbox already shows the prompt */
#sel-caption {display: none !important;}
/* run-cards read as small floating toasts over the grid */
#canvas-footer .run-card, #canvas-footer .run-done {
    background: rgba(17,15,26,.94);
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    box-shadow: 0 10px 28px rgba(0,0,0,.5);
    padding: 8px 12px;
    border-radius: 12px;
    border: 1px solid rgba(139,92,246,.25);
}
#canvas-footer .run-done {cursor: pointer;}   /* click a finished-run toast to dismiss it */
/* selection pill moves to the top-LEFT so it never sits under the View button */
#ms-pill {left: 18px; right: auto;}

/* ---------- mobile (≤760px): touch-first layout ---------- */
@media (max-width: 760px) {
    .gradio-container > .main {padding: 8px 8px 4px 8px !important;}
    h1.app-title {font-size: 1.1rem; flex: none;}
    #topbar, #projectbar {flex-wrap: wrap !important; row-gap: 6px !important;}
    /* basis 0, not auto: with flex-wrap, line-breaking uses the BASIS — auto
       (= full content width) forced the dropdown onto its own row */
    #project-dd {min-width: 120px !important; flex: 1 1 0 !important;}
    /* v4.9: 2-row top bar — JS (compactTopbarMobile) orders the flex items:
       row 1 = logo + project, row 2 = spend (left) + Guide/⧦/⚙ (right) */
    #topstats {font-size: .7rem; text-align: left;}
    /* Guide + Settings collapse to round icon buttons so row 2 always fits */
    #setup-open-btn, #settings-open-btn {
        font-size: 0 !important;
        width: 34px !important;
        min-width: 34px !important;
        max-width: 34px !important;
        height: 34px !important;
        padding: 0 !important;
        border-radius: 999px !important;
        display: inline-flex !important;
        align-items: center !important;
        justify-content: center !important;
    }
    #setup-open-btn::before, #settings-open-btn::before {margin-right: 0 !important;}
    /* two big columns beat five tiny ones on a phone, whatever ▦ says */
    #result-gallery .grid-container {grid-template-columns: repeat(2, minmax(0, 1fr)) !important;}
    /* phones (v4.9): the grid grows to its full content and the PAGE scrolls —
       no internal scrollbox, no dead gap; the last row scrolls clear of the
       fixed pill via the bottom padding. (Lasso is desktop-only, so grid-wrap
       doesn't need to stay the scroll container here.) */
    #result-gallery .grid-wrap {height: auto !important; max-height: none !important; padding-top: 8px !important; padding-bottom: 116px !important;}
    #canvas-card .row {flex-wrap: wrap !important; row-gap: 6px !important;}
    /* phones: keep the footer in normal flow (below the grid) — simpler than
       floating it above a variable-height wrapped composer */
    #canvas-footer {position: static !important; bottom: auto !important; width: auto !important; max-width: none !important; margin: 0 !important; padding: 4px 4px 0 4px !important;}
    /* sit above the iPhone home bar, never under it */
    #composer {padding: 8px 10px !important; left: 6px !important; right: 6px !important; bottom: calc(6px + env(safe-area-inset-bottom, 0px)) !important;}
    #composer .row {flex-wrap: wrap !important; row-gap: 8px !important;}
    /* v4.9: the settings pop-up becomes a real BOTTOM SHEET on phones. As an
       absolute panel above the fixed pill it extended past the top of the screen
       and looked like it "didn't show up" — fixed positioning escapes that. */
    #settings-pop {
        position: fixed !important;
        left: 6px !important;
        right: 6px !important;
        top: auto !important;
        bottom: calc(6px + env(safe-area-inset-bottom, 0px)) !important;
        max-height: 78vh;
        overflow-y: auto !important;
        z-index: 80 !important;
        border-radius: 20px !important;
    }
    /* iPhones auto-zoom into inputs under 16px — keep typing zoom-free */
    #prompt-box textarea, #projectbar input[type="text"] {font-size: 16px !important;}
    /* the paste tile is pointless on phones (Ctrl+V / drag & drop still work) */
    #paste-box {display: none !important;}
    /* phones (v4.9, slimmer): prompt gets its own line, then ONE tidy row of
       ＋ · model · → — the model pill shrinks (ellipsis) so → never wraps to a
       third row, and the prompt line is a single text row, not a tall box */
    /* everything is compact now — ONE row, like desktop (nowrap kills the
       phantom wraps Gradio's wrapper min-widths kept causing) */
    #composer #pill-row {flex-wrap: nowrap !important; row-gap: 6px !important; border-radius: 20px !important; padding: 8px !important;}
    /* v6.0: while you're typing, the prompt takes its own full-width line above the
       ＋ / model / → row so the whole prompt is readable (setupPromptGrow) */
    #composer #pill-row.prompt-grown {flex-wrap: wrap !important;}
    #composer #pill-row.prompt-grown #prompt-box {flex: 1 1 100% !important; min-width: 100% !important;}
    #composer #pill-row.prompt-grown #model-pill {flex: 1 1 auto !important; max-width: none !important;}
    #composer #prompt-box textarea {transition: height .12s ease;}
    #composer #prompt-box {flex: 1 1 0 !important; min-width: 60px !important; order: -1;}
    #composer #prompt-box textarea {min-height: 24px !important; max-height: 92px !important; padding: 4px 6px !important;}
    #composer #prompt-box .wrap, #composer #prompt-box .block, #composer #prompt-box .form {min-height: 0 !important; padding: 0 !important;}
    #composer #add-files-btn, #composer #add-files-btn button {order: 1; min-width: 36px !important; width: 36px !important; height: 36px !important;}
    #composer #model-pill {
        order: 2;
        flex: 0 1 auto !important;      /* may shrink; .mp-text ellipsises, price stays */
        min-width: 0 !important;
        max-width: 62vw !important;     /* v6.0: room for the ≈$ estimate */
        height: 36px !important;
        flex-wrap: nowrap !important;   /* label + caret stay on ONE line */
        display: inline-flex !important;
    }
    #composer #go-btn {order: 3; min-width: 36px !important; width: 36px !important; height: 36px !important;}
    /* v4.0: the Image/Video toggle gets its own full-width line of equal pills
       (#composer prefix outguns the 47% settings-grid rule below) */
    #composer #media-toggle {flex: 1 1 100% !important; min-width: 100% !important; max-width: 100% !important; order: -1;}
    #media-toggle .wrap {width: 100%;}
    #media-toggle label {flex: 1; display: flex; justify-content: center; min-height: 40px; align-items: center;}
    /* v4.0: video mode pills full-width; frame slots side by side */
    #video-row {flex-wrap: wrap !important; row-gap: 8px !important;}
    #vmode-radio {flex: 1 1 100% !important; min-width: 100% !important;}
    #vmode-radio .wrap {width: 100%;}
    #vmode-radio label {flex: 1; display: flex; justify-content: center; min-height: 40px; align-items: center;}
    #frame-start, #frame-end {flex: 1 1 47% !important; min-width: 47% !important; max-width: none;}
    #filter-dd {min-width: 104px !important;}
    /* settings row becomes a tidy 2-per-line grid; ratio preview is skipped */
    #composer .row:has(input[aria-expanded]) > * {
        flex: 1 1 47% !important;
        min-width: 47% !important;
        max-width: 100%;
    }
    /* v4.9: the old ratio-preview hider here was `#composer .column:has(#ratio-box)`
       — but #settings-pop is ITSELF a .column containing #ratio-box, so that rule
       display:none'd the ENTIRE settings panel on phones ("the dropdown doesn't
       show up on mobile"). The desktop rule (#settings-row #ratio-box) already
       hides the preview everywhere, so nothing is needed here. */
    #mode-hint {display: none;}
    button.sm {min-height: 38px;}
    #ingredient-tray .grid-container {
        grid-template-columns: repeat(auto-fill, 74px) !important;
        grid-auto-rows: 74px !important;
    }
    #ingredient-tray .gallery-item, #ingredient-tray .thumbnail-item {
        width: 74px !important;
        height: 74px !important;
    }
    #nb-lightbox .lb-actions {gap: 5px;}
    #nb-lightbox .lb-actions button {width: 32px; height: 32px; font-size: 13px;}
    #nb-lightbox .lb-name {font-size: .68rem; max-width: 30vw; padding: 5px 10px;}
    #nb-lightbox .lb-nav {width: 38px; height: 38px; font-size: 19px; opacity: .8;}
    #nb-lightbox .lb-nav.prev {left: 6px;}
    #nb-lightbox .lb-nav.next {right: 6px;}
    #nb-lightbox .lb-strip img, #nb-lightbox .lb-strip video {width: 36px; height: 36px;}
    #empty-hint {font-size: 1rem;}
    #empty-hint span {font-size: .72rem; max-width: 86vw;}
    #setup-wrap {width: 96vw; height: 92vh;}
}

.toast-wrap, .toast-body {z-index: 9999 !important; pointer-events: auto !important;}
footer {display: none !important;}
"""

# Runs once in the browser when the page loads: forces dark mode, then wires up
# multi-select (drag lasso, Ctrl/Shift click, Ctrl+A / Esc / Del shortcuts).
FRONTEND_JS = r"""function bananaBoot() {
    if (window.__bananaBooted) return;
    window.__bananaBooted = true;

    // v6.4: dark mode is handled by a server-side redirect to ?__theme=dark now
    // (see _media_cache_app_kwargs), so the app loads ONCE, already dark. This used
    // to be `window.location.href = …` right here, which meant every visit from a
    // bookmark or share link loaded the entire app and then immediately reloaded it,
    // throwing away the canvas and anything unsaved in the composer. The class below
    // is only a belt-and-braces no-op for the case where that middleware couldn't be
    // installed; it is NOT sufficient on its own — Gradio's own theming wins.
    document.body.classList.add('dark');

    // v5.0: block accidental page zoom (double-tap / pinch-in on rapid button taps).
    // Gradio ships `initial-scale=1`; add maximum-scale=1 so the page can never zoom
    // IN. The fullscreen lightbox zoom is a CSS transform, not browser zoom, so it's
    // unaffected. Rewritten (not appended) so there's exactly one viewport meta.
    (function lockViewport() {
        let m = document.querySelector('meta[name="viewport"]');
        if (!m) { m = document.createElement('meta'); m.name = 'viewport'; document.head.appendChild(m); }
        m.setAttribute('content',
            'width=device-width, initial-scale=1, maximum-scale=1, viewport-fit=cover');
    })();

    const $ = (s, r) => (r || document).querySelector(s);
    const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
    const gridWrap = () => $('#result-gallery .grid-wrap');
    const previewOpen = () => !!(lbEl && lbEl.isConnected);

    const isTouch = !!(window.matchMedia && matchMedia('(pointer: coarse)').matches);
    const picked = new Set();   // basenames of selected files
    let anchor = null;          // last clicked name, for Shift ranges
    let selMode = false;        // v4.9 touch: "Select" chip mode — taps toggle selection
    let lpTimer = null, lpFired = false, lpFiredAt = 0;   // long-press → actions (touch)
    let suppressClick = false;  // eat the click that ends a lasso drag
    let band = null, drag = null;

    // Write a value into an input the way the framework expects (native value
    // setter + input/change events) so Gradio's frontend state always sees it.
    function setNative(inp, v) {
        const proto = inp.tagName === 'TEXTAREA'
            ? window.HTMLTextAreaElement.prototype
            : window.HTMLInputElement.prototype;
        const d = Object.getOwnPropertyDescriptor(proto, 'value');
        if (d && d.set) d.set.call(inp, v); else inp.value = v;
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
    }

    function baseName(src) {
        try {
            let p = src.split('file=')[1] || new URL(src, location.href).pathname;
            p = decodeURIComponent(p).split('?')[0];
            const parts = p.split(/[\\/]/);
            return parts[parts.length - 1] || null;
        } catch (e) { return null; }
    }
    function thumbs() {
        const g = gridWrap();
        return g ? $$('.thumbnail-item', g) : [];
    }
    // v3.9: canvas items can be <img> OR <video> — always go through these
    function mediaEl(t) { return t.querySelector('img, video'); }
    function srcOf(el) {
        if (!el) return null;
        if (el.tagName === 'VIDEO') {
            const so = el.querySelector && el.querySelector('source');
            return el.currentSrc || el.src || (so && so.src) || null;
        }
        return el.src || null;
    }
    function isVid(name) { return /\.mp4$/i.test(name || ''); }
    // v6.0 PERF: memoized per element+src. paint() calls this for every tile on every
    // repaint; re-parsing the URL each time was pure waste with a full project open.
    function thumbName(t) {
        const src = srcOf(mediaEl(t));
        if (!src) return null;
        if (t.dataset.bzSrc === src) return t.dataset.bzName || null;
        const n = baseName(src);
        t.dataset.bzSrc = src;
        t.dataset.bzName = n || '';
        return n;
    }
    function syncStore() {
        const store = $('#multi-sel-store textarea') || $('#multi-sel-store input');
        if (!store) return;
        const v = JSON.stringify(Array.from(picked));
        if (store.value === v) return;
        setNative(store, v);
    }
    let pillFlashUntil = 0;
    function getPill() {
        const card = $('#canvas-card');
        if (!card) return null;
        let pill = $('#ms-pill');
        if (!pill) {
            pill = document.createElement('div');
            pill.id = 'ms-pill';
            pill.addEventListener('click', (e) => {
                const b = e.target.closest && e.target.closest('.pill-act');
                if (!b) return;
                e.stopPropagation();
                if (b.dataset.a === 'clear') clearPicks(); else clickDeleteBtn();
            });
            card.appendChild(pill);
        }
        return pill;
    }
    function flashPill(txt) {
        const pill = getPill();
        if (!pill) return;
        pill.dataset.h = '';
        pill.textContent = txt;
        pill.classList.add('on');
        pill.classList.remove('touchy');
        pillFlashUntil = Date.now() + 1400;
        setTimeout(() => { pillFlashUntil = 0; updatePill(); }, 1500);
    }
    // v4.9 touch: explicit Select mode — a "☑ Select" chip top-left of the grid.
    // Tap it, then tap tiles to pick several; "✕ Done" leaves (and clears).
    let selChip = null;
    function getSelChip() {
        if (!isTouch) return null;
        if (!selChip) {
            const card = $('#canvas-card');
            if (!card) return null;
            selChip = document.createElement('button');
            selChip.id = 'sel-chip';
            selChip.type = 'button';
            selChip.textContent = '☑ Select';
            selChip.addEventListener('click', (e) => {
                e.stopPropagation();
                e.preventDefault();
                selMode = !selMode;
                selChip.classList.toggle('on', selMode);
                selChip.textContent = selMode ? '✕ Done' : '☑ Select';
                if (!selMode) clearPicks();
            });
            card.appendChild(selChip);
        }
        return selChip;
    }
    function updatePill() {
        if (Date.now() < pillFlashUntil) return;
        const pill = getPill();
        if (!pill) return;
        const n = picked.size;
        if (!n) {
            pill.classList.remove('on', 'touchy');
            pill.dataset.h = '';
            if (pill.textContent) pill.textContent = '';
            return;
        }
        // v4.7: the bottom "Delete selected" button is gone — the pill carries
        // the actions everywhere (Flow-style contextual bar). Del/Esc still work.
        const h = n + " selected"
            + "<button class='pill-act' data-a='clear' title='Esc also clears'>Clear</button>"
            + "<button class='pill-act pill-del' data-a='del' title='Del key also deletes'>🗑 Delete</button>";
        if (pill.dataset.h !== h) { pill.innerHTML = h; pill.dataset.h = h; }
        pill.classList.add('on', 'touchy');
        // touch: shift right of the Select chip so the two never overlap
        const chip = getSelChip();
        if (chip) pill.style.left = (18 + chip.offsetWidth + 8) + 'px';
    }
    function paint() {
        const present = new Set();
        thumbs().forEach(t => {
            const n = thumbName(t);
            if (n) present.add(n);
            t.classList.toggle('ms-picked', !!n && picked.has(n));
            // v4.9: unmissable ▶ badge on video tiles (class, not :has() — the
            // CSS scoper mangles :has())
            t.classList.toggle('vid-tile', !!n && isVid(n));
        });
        let changed = false;
        picked.forEach(n => { if (!present.has(n)) { picked.delete(n); changed = true; } });
        if (changed) syncStore();
        updatePill();
        const chip = getSelChip();
        if (chip) chip.style.display = present.size ? 'inline-block' : 'none';
        lbSync();
        // friendly empty state when the project has no images yet
        const card = $('#canvas-card');
        if (card) {
            let hint = $('#empty-hint');
            const none = present.size === 0;
            if (none && !hint) {
                hint = document.createElement('div');
                hint.id = 'empty-hint';
                hint.innerHTML = 'Nothing here yet<span>Describe what you want in the bar below '
                    + 'and press Generate \u2014 images land right here as they finish.</span>';
                card.appendChild(hint);
            }
            if (hint) hint.style.display = none ? '' : 'none';
        }
    }
    function clearPicks() {
        picked.clear();
        anchor = null;
        syncStore();
        paint();
    }

    // Composer menus: Gradio sizes them for the space BELOW the input, which is
    // ~40px at the screen bottom — so menus collapsed to one visible item.
    // Re-assert the geometry with inline !important (beats Gradio's inline styles).
    function fixComposerMenus() {
        $$('#composer ul.options').forEach((ul) => {
            const s = ul.style;
            // already ours? skip so the style-observer doesn't loop
            if (s.getPropertyPriority('bottom') === 'important' && s.top === 'auto') return;
            s.setProperty('position', 'absolute', 'important');
            s.setProperty('top', 'auto', 'important');
            s.setProperty('bottom', 'calc(100% + 6px)', 'important');
            s.setProperty('left', '0', 'important');
            s.setProperty('max-height', 'min(45vh, 460px)', 'important');
            s.setProperty('height', 'auto', 'important');
            s.setProperty('overflow-y', 'auto', 'important');
            s.setProperty('width', 'max-content', 'important');
            s.setProperty('min-width', '100%', 'important');
        });
        $$('#projectbar ul.options').forEach((ul) => {
            if (ul.style.getPropertyPriority('max-height') === 'important') return;
            ul.style.setProperty('max-height', '45vh', 'important');
            ul.style.setProperty('height', 'auto', 'important');
        });
        // Gradio 6.20 wraps blocks in overflow:hidden containers that clip open
        // menus to a sliver — un-clip every ancestor of an open menu, up to the
        // app root. (The dropdown chains never include the gallery scroller.)
        $$('ul.options').forEach((ul) => {
            let el = ul.parentElement;
            while (el && el !== document.body && !el.classList.contains('gradio-container')) {
                if (el.style.getPropertyValue('overflow') !== 'visible') {
                    const cs = getComputedStyle(el);
                    if (cs.overflow !== 'visible' || cs.overflowY !== 'visible') {
                        el.style.setProperty('overflow', 'visible', 'important');
                    }
                }
                el = el.parentElement;
            }
        });
    }

    // Canvas cards, stylesheet-proof: enforce natural-aspect uncropped geometry
    // inline on every thumbnail (new nodes get it on each re-render).
    function enforceCardFit() {
        const g = gridWrap();
        if (!g) return;
        const gc = g.querySelector('.grid-container');
        if (gc && !gc.dataset.fitCols) {
            gc.dataset.fitCols = '1';
            gc.style.setProperty('grid-template-rows', 'none', 'important');
            gc.style.setProperty('grid-auto-rows', 'max-content', 'important');
            gc.style.setProperty('align-content', 'start', 'important');
        }
        if (gc) {
            // Gradio collapses the grid to the item count (1 image → 1 giant column).
            // Force the chosen column count so every tile stays a normal Flow size.
            const gd = document.querySelector('#grid-dd input');
            let n = gd ? parseInt((gd.value || '').replace(/\D/g, ''), 10) : 0;
            if (!n || n < 1) n = 5;
            gc.style.setProperty('--bs-cols', n);
        }
        thumbs().forEach((t) => {
            if (t.dataset.fitApplied) return;
            t.dataset.fitApplied = '1';
            // Natural-aspect tiles: the image fills the column width (no side bars,
            // no crop); tall shots just make taller tiles.
            t.style.setProperty('height', 'auto', 'important');
            t.style.setProperty('aspect-ratio', 'auto', 'important');
            t.style.setProperty('max-height', 'none', 'important');
            const gi = t.closest('.gallery-item');
            if (gi) gi.style.setProperty('height', 'auto', 'important');
            const im = mediaEl(t);
            if (im) {
                im.style.setProperty('width', '100%', 'important');
                im.style.setProperty('height', 'auto', 'important');
                im.style.setProperty('max-height', 'none', 'important');
                im.style.setProperty('object-fit', 'contain', 'important');
                if (im.tagName === 'VIDEO') {
                    // Gradio ships gallery <video> with no preload/playsinline, so
                    // iOS painted a BLACK tile until some later re-render. Load
                    // metadata and nudge past 0 to force a real first frame.
                    // (Files are cached hard by the server now — see the
                    // Cache-Control middleware — so this costs nothing on a revisit.)
                    im.muted = true;
                    im.playsInline = true;
                    im.setAttribute('playsinline', '');
                    im.preload = 'metadata';
                    im.addEventListener('loadedmetadata', () => {
                        try { if (!im.currentTime) im.currentTime = 0.05; } catch (e) {}
                    }, { once: true });
                    if (im.readyState >= 1) { try { im.currentTime = 0.05; } catch (e) {} }
                    else { try { im.load(); } catch (e) {} }
                } else {
                    // v6.0 perf: off-screen tiles cost nothing until scrolled to
                    im.loading = 'lazy';
                    im.decoding = 'async';
                }
            }
        });
    }

    // v6.0 (mobile): the prompt stays a slim single line at rest, but grows to fit
    // what you're typing (up to ~4 lines) so long prompts are readable — then snaps
    // back when you leave it empty. Nothing else on screen is covered: #pill-row is
    // allowed to wrap only while the box is tall.
    function setupPromptGrow() {
        if (!mobileUi) return;
        const ta = document.querySelector('#prompt-box textarea');
        const row = document.getElementById('pill-row');
        if (!ta || !row || ta.dataset.bzGrow) return;
        ta.dataset.bzGrow = '1';
        const MAXH = 112;                       // ≈4 lines, then it scrolls internally
        const resize = () => {
            const grown = ta.value.trim().length > 0 || document.activeElement === ta;
            if (!grown) {
                ta.style.height = '';
                row.classList.remove('prompt-grown');
                return;
            }
            ta.style.height = 'auto';
            const h = Math.min(ta.scrollHeight, MAXH);
            ta.style.height = h + 'px';
            ta.style.overflowY = ta.scrollHeight > MAXH ? 'auto' : 'hidden';
            row.classList.toggle('prompt-grown', h > 30);
        };
        ['input', 'focus', 'blur'].forEach((ev) => ta.addEventListener(ev, resize));
        resize();
    }

    // v4.9: phones — 2-row top bar: [logo · project] / [spend … Guide ⧦ ⚙].
    // Done in JS because the flex items are a mix of Gradio wrappers and bare
    // buttons (incl. the late-inserted View toggle); idempotent, runs in the
    // observer chain so late arrivals get ordered too.
    const mobileUi = window.matchMedia('(max-width: 760px)').matches;
    function compactTopbarMobile() {
        if (!mobileUi) return;
        const tb = document.getElementById('topbar');
        if (!tb) return;
        // hard line break between [logo · project] and [spend · buttons] —
        // without it, basis-0 items just pack onto the first row
        if (!document.getElementById('tb-break')) {
            const br = document.createElement('div');
            br.id = 'tb-break';
            br.style.cssText = 'order:5;flex-basis:100%;height:0;margin:0;padding:0';
            tb.appendChild(br);
        }
        Array.from(tb.children).forEach((ch) => {
            if (ch.id === 'tb-break') return;                   // keep its order:5
            let order = 20;                                     // buttons → row 2, right
            if (ch.querySelector && ch.querySelector('h1.app-title')) {
                order = 0;
                // Gradio markdown blocks carry min-width:min(320px,100%) — that
                // alone pushed the project dropdown to its own row
                if (ch.style.minWidth !== '0px') { ch.style.minWidth = '0'; ch.style.flex = '0 0 auto'; ch.style.width = 'auto'; }
            } else if (ch.id === 'project-dd' || (ch.querySelector && ch.querySelector('#project-dd'))) {
                order = 1;
                // basis 0 — flex-wrap breaks lines on the BASIS, and auto = content
                if (ch.style.flex !== '1 1 0%') { ch.style.flex = '1 1 0%'; ch.style.minWidth = '0'; }
            } else if (ch.id === 'topstats-wrap') {
                order = 10;                                     // spend → row 2, left
                if (ch.style.flex !== '1 1 0%') { ch.style.flex = '1 1 0%'; ch.style.marginLeft = '0'; ch.style.minWidth = '0'; }
            }
            if (ch.style.order !== String(order)) ch.style.order = order;
        });
    }

    // v4.7: the folder count ("📁 N") lives in the top bar now — JS keeps a small
    // span at the front of #topstats in sync with what the grid actually shows.
    // Gradio replaces #topstats' HTML on spend refreshes; the observer below
    // re-inserts the span the moment that happens.
    function updateFoldCount() {
        const ts = $('#topstats');
        if (!ts) return;
        let el = document.getElementById('fold-count');
        if (!el) {
            el = document.createElement('span');
            el.id = 'fold-count';
            ts.prepend(el);
        }
        const t = '📁 ' + thumbs().length + ' · ';
        if (el.textContent !== t) el.textContent = t;
    }
    // finished-run toasts (bottom-left) dismiss on click
    document.addEventListener('click', (e) => {
        const d = e.target.closest && e.target.closest('#canvas-footer .run-done');
        if (d) d.remove();
    });

    // repaint whenever Gradio re-renders (throttled to one frame);
    // also covers dropdown menus the moment they open.
    // v6.0 PERF: Gradio mutates style attributes constantly (progress bars, menus,
    // its own re-renders). Running the full batch on every one of those was the main
    // source of lag on slower laptops. Now we (a) do nothing while the tab is hidden
    // and (b) skip mutations that can't affect anything we manage — same behaviour,
    // a fraction of the work.
    let rafPending = false;
    const WATCHED = '#canvas-card, #composer, #topbar, #settings-wrap, ul.options';
    function relevant(records) {
        for (const r of records) {
            const n = r.target;
            if (!n || n.nodeType !== 1) continue;
            if (n.closest && n.closest(WATCHED)) return true;
            // new nodes anywhere (a gallery re-render replaces whole subtrees)
            if (r.addedNodes && r.addedNodes.length) return true;
        }
        return false;
    }
    new MutationObserver((records) => {
        if (rafPending || document.hidden) return;
        if (!relevant(records)) return;
        rafPending = true;
        requestAnimationFrame(() => {
            rafPending = false;
            paint(); fixComposerMenus(); enforceCardFit(); setupFlowChrome();
            updateFoldCount(); compactTopbarMobile(); setupPromptGrow();
        });
    }).observe(document.body, { childList: true, subtree: true, attributes: true,
                                attributeFilter: ['style', 'aria-expanded'] });

    // never let the browser start dragging the image files themselves
    document.addEventListener('dragstart', (e) => {
        const g = gridWrap();
        if (g && g.contains(e.target)) e.preventDefault();
    }, true);

    // ---- Ctrl/Shift click (capture phase, so nothing else steals it) ----
    document.addEventListener('click', (e) => {
        if (e.target.closest && e.target.closest('.thumb-actions, #nb-lightbox, #ms-pill')) return;
        if (lpFired) {   // eat the click a long-press leaves behind…
            lpFired = false;
            // …but ONLY a fresh one ON A TILE. iOS doesn't always deliver that click,
            // and a stale flag (or a tap that lands on chrome like the Select chip)
            // must never be swallowed — that made quick chip taps feel dead.
            if (Date.now() - lpFiredAt < 700 && e.target.closest &&
                e.target.closest('#result-gallery .thumbnail-item')) {
                e.stopPropagation();
                e.preventDefault();
                return;
            }
        }
        // v4.9 touch: any other tap puts the long-press action cluster away
        // (must live AFTER the lpFired eater so the cluster survives its own birth tap)
        if (isTouch && actions.classList.contains('show') && !actions.contains(e.target)) {
            actions.classList.remove('show');
        }
        const g = gridWrap();
        if (!g || !g.contains(e.target)) return;
        if (suppressClick) {
            e.stopPropagation();
            e.preventDefault();
            suppressClick = false;
            return;
        }
        const t = e.target.closest('.thumbnail-item');
        if (!t) {
            if (picked.size && !e.ctrlKey && !e.metaKey && !e.shiftKey) clearPicks();
            return;
        }
        const name = thumbName(t);
        if (e.ctrlKey || e.metaKey) {
            e.stopPropagation();
            e.preventDefault();
            if (picked.has(name)) picked.delete(name); else picked.add(name);
            anchor = name;
            syncStore();
            paint();
        } else if (e.shiftKey) {
            e.stopPropagation();
            e.preventDefault();
            const all = thumbs().map(thumbName);
            let a = all.indexOf(anchor), b = all.indexOf(name);
            if (a === -1) a = b;
            const lo = Math.min(a, b), hi = Math.max(a, b);
            for (let i = lo; i <= hi; i++) if (all[i]) picked.add(all[i]);
            syncStore();
            paint();
        } else if (isTouch && (selMode || picked.size)) {
            // selection mode on touch: taps toggle instead of opening the viewer
            e.stopPropagation();
            e.preventDefault();
            if (picked.has(name)) picked.delete(name); else picked.add(name);
            anchor = name;
            syncStore();
            paint();
        } else {
            anchor = name;
            if (picked.size) clearPicks();
            openLightbox(name);
        }
    }, true);

    // ---- drag lasso ----
    document.addEventListener('mousedown', (e) => {
        if (e.button !== 0) return;
        const g = gridWrap();
        if (!g || !g.contains(e.target) || previewOpen()) return;
        drag = {
            startX: e.clientX,
            startY: e.clientY,
            addTo: e.ctrlKey || e.metaKey || e.shiftKey,
            base: new Set(picked),
            active: false, cx: 0, cy: 0,
        };
    }, true);

    document.addEventListener('mousemove', (e) => {
        if (!drag) return;
        const g = gridWrap();
        if (!g) { drag = null; return; }
        const dx = e.clientX - drag.startX, dy = e.clientY - drag.startY;
        if (!drag.active && (dx * dx + dy * dy) < 36) return;  // 6px threshold
        const r = g.getBoundingClientRect();
        if (!drag.active) {
            drag.active = true;
            document.body.classList.add('ms-dragging');
            drag.cx = drag.startX - r.left + g.scrollLeft;
            drag.cy = drag.startY - r.top + g.scrollTop;
            if (!band) {
                band = document.createElement('div');
                band.id = 'ms-band';
            }
        }
        if (!band.isConnected) g.appendChild(band);
        // auto-scroll the gallery when lassoing near its edges
        if (e.clientY > r.bottom - 34) g.scrollTop += 16;
        else if (e.clientY < r.top + 34 && g.scrollTop > 0) g.scrollTop -= 16;
        const px = e.clientX - r.left + g.scrollLeft;
        const py = e.clientY - r.top + g.scrollTop;
        const x1 = Math.min(drag.cx, px), y1 = Math.min(drag.cy, py);
        const x2 = Math.max(drag.cx, px), y2 = Math.max(drag.cy, py);
        band.style.left = x1 + 'px';
        band.style.top = y1 + 'px';
        band.style.width = (x2 - x1) + 'px';
        band.style.height = (y2 - y1) + 'px';
        picked.clear();
        drag.addTo && drag.base.forEach(n => picked.add(n));
        thumbs().forEach(t => {
            const tr = t.getBoundingClientRect();
            const tx1 = tr.left - r.left + g.scrollLeft, ty1 = tr.top - r.top + g.scrollTop;
            if (tx1 < x2 && tx1 + tr.width > x1 && ty1 < y2 && ty1 + tr.height > y1) {
                const n = thumbName(t);
                if (n) picked.add(n);
            }
        });
        paint();
        e.preventDefault();
    }, true);

    document.addEventListener('mouseup', () => {
        if (!drag) return;
        if (drag.active) {
            suppressClick = true;
            setTimeout(() => { suppressClick = false; }, 150);
            syncStore();
        }
        if (band) band.remove();
        document.body.classList.remove('ms-dragging');
        drag = null;
        paint();
    }, true);

    // ---- keyboard: Ctrl+A select all · Esc clear · Del delete selection ----
    document.addEventListener('keydown', (e) => {
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
        if (previewOpen()) {
            if (e.key === 'Escape') { e.preventDefault(); closeLightbox(); }
            else if (e.key === 'ArrowLeft') { e.preventDefault(); lbNav(-1); }
            else if (e.key === 'ArrowRight') { e.preventDefault(); lbNav(1); }
            else if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); lbDelete(); }
            else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c') {
                if (window.getSelection && String(window.getSelection())) return;
                e.preventDefault();
                if (isVid(lbName)) { lbFlash('Videos: use the \u2913 download button'); return; }
                copyImage(lbName).then((ok) => lbFlash(ok ? 'Copied to clipboard \u2713' : 'Copy failed \u2715'));
            }
            return;
        }
        if (e.key === 'Escape') {
            if (picked.size) { clearPicks(); e.preventDefault(); }
        } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
            const names = thumbs().map(thumbName).filter(Boolean);
            if (!names.length) return;
            e.preventDefault();
            names.forEach(n => picked.add(n));
            syncStore();
            paint();
        } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'c' && picked.size === 1) {
            if (window.getSelection && String(window.getSelection())) return;  // real text copy
            e.preventDefault();
            const one = Array.from(picked)[0];
            if (isVid(one)) { flashPill('Videos: open one and use \u2913 to save'); return; }
            copyImage(one).then((ok) => {
                flashPill(ok ? 'Copied to clipboard ✓' : 'Copy failed ✕');
            });
        } else if ((e.key === 'Delete' || e.key === 'Backspace') && picked.size) {
            e.preventDefault();
            clickDeleteBtn();
        }
    });

    function clickDeleteBtn() {
        const host = $('#del-img-btn');
        if (!host) return;
        const btn = host.tagName === 'BUTTON' ? host : host.querySelector('button');
        if (btn) btn.click();
    }

    // ---- bridge to Python: write the payload into a ghost textbox, then click
    // a ghost button. Textbox value-sync + button clicks are the two frontend
    // mechanisms Gradio 6.20 reliably delivers (synthetic .input events on a
    // textbox do NOT trigger Python .input listeners in 6.20). ----
    function fireStore(id, payload) {
        const host = document.getElementById(id);
        const inp = host && (host.querySelector('textarea') || host.querySelector('input'));
        if (!inp) return;
        setNative(inp, JSON.stringify(Object.assign({ t: Date.now() }, payload)));
    }
    function clickHidden(id) {
        const host = document.getElementById(id);
        const btn = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        if (btn) btn.click();
    }
    function storeThenClick(storeId, payload, btnId) {
        fireStore(storeId, payload);
        setTimeout(() => clickHidden(btnId), 30);
    }

    // ---- copy a finished image to the clipboard (re-encoded as PNG) ----
    function copyImage(name) {
        if (isVid(name)) return Promise.resolve(false);  // no video clipboards— use \u2913 download
        const t = thumbs().find((x) => thumbName(x) === name);
        const img = t && t.querySelector('img');
        if (!img) return Promise.resolve(false);
        if (!navigator.clipboard || !navigator.clipboard.write || !window.ClipboardItem) {
            return Promise.resolve(false);
        }
        // v5.2 iOS-CORRECT copy: Safari/iOS strips the user activation if you `await`
        // the blob BEFORE calling clipboard.write(). Call write() SYNCHRONOUSLY inside
        // the tap, handing ClipboardItem a Promise<Blob> so the fetch/encode happens
        // while the gesture is still valid. Without this the copy "succeeds" silently
        // but nothing reaches the system clipboard (couldn't paste into iMessage).
        // canvas re-encode → PNG (same-origin src, so never tainted).
        function toPngBlob() {
            return fetch(img.src)
                .then((r) => r.blob())
                .then((blob) => new Promise((res, rej) => {
                    const url = URL.createObjectURL(blob);
                    const im = new Image();
                    im.onload = () => {
                        const c = document.createElement('canvas');
                        c.width = im.naturalWidth;
                        c.height = im.naturalHeight;
                        c.getContext('2d').drawImage(im, 0, 0);
                        URL.revokeObjectURL(url);
                        c.toBlob((b) => (b ? res(b) : rej(new Error('encode failed'))), 'image/png');
                    };
                    im.onerror = () => { URL.revokeObjectURL(url); rej(new Error('load failed')); };
                    im.src = url;
                }));
        }
        try {
            const item = new ClipboardItem({ 'image/png': toPngBlob() });
            return navigator.clipboard.write([item])
                .then(() => true)
                .catch((e) => { console.warn('copy to clipboard failed', e); return false; });
        } catch (e) {
            console.warn('ClipboardItem unsupported', e);
            return Promise.resolve(false);
        }
    }

    // ---- custom lightbox: click a finished image to view it big.
    // Gradio's own preview is disabled (allow_preview=False) — its 6.20 markup
    // is a fragile carousel that breaks under styling; this one is ours. ----
    let lbEl = null, lbName = null, lbFlashTimer = null;
    const lbZoom = { z: 1, tx: 0, ty: 0, name: null };   // v4.9 fullscreen zoom state
    function lbNames() { return thumbs().map(thumbName).filter(Boolean); }
    function lbSrcFor(name) {
        const t = thumbs().find((x) => thumbName(x) === name);
        return t ? srcOf(mediaEl(t)) : null;
    }
    function buildLightbox() {
        lbEl = document.createElement('div');
        lbEl.id = 'nb-lightbox';
        lbEl.innerHTML =
            '<div class="lb-top"><span class="lb-name"></span><div class="lb-actions">'
            + '<button class="lb-reuse" title="Reuse this image\u2019s prompt & settings">\u21BB</button>'
            + '<button class="lb-ing" title="Add to ingredients (edit or combine it)">+</button>'
            + '<button class="lb-copy" title="Copy to clipboard (images only)">\u{1F4CB}</button>'
            + '<button class="lb-down" title="Download">\u2913</button>'
            + '<button class="lb-del" title="Delete this image">' + TRASH_SVG + '</button>'
            + '<button class="lb-close" title="Close (Esc)">\u2715</button></div></div>'
            + '<button class="lb-nav prev" title="Previous (\u2190)">\u2039</button>'
            + '<img class="lb-img" alt="">'
            + '<video class="lb-vid" controls playsinline loop style="display:none"></video>'
            + '<button class="lb-nav next" title="Next (\u2192)">\u203A</button>'
            + '<div class="lb-strip"></div>';
        lbEl.addEventListener('click', (e) => { if (e.target === lbEl) closeLightbox(); });
        lbEl.querySelector('.lb-close').addEventListener('click', closeLightbox);
        lbEl.querySelector('.lb-nav.prev').addEventListener('click', () => lbNav(-1));
        lbEl.querySelector('.lb-nav.next').addEventListener('click', () => lbNav(1));
        lbEl.querySelector('.lb-reuse').addEventListener('click', () => {
            storeThenClick('reuse-store', { n: lbName }, 'reuse-btn');
            lbFlash('Prompt & settings restored \u2713');
        });
        lbEl.querySelector('.lb-ing').addEventListener('click', () => {
            if (isVid(lbName)) {
                lbFlash('Videos can\u2019t be ingredients \u2014 \u21BB reuses the prompt');
                return;
            }
            storeThenClick('ingredient-store', { n: lbName }, 'ingredient-btn');
            lbFlash('Added to ingredients \u2713');
        });
        lbEl.querySelector('.lb-copy').addEventListener('click', (e) => {
            const b = e.currentTarget;
            if (isVid(lbName)) { lbFlash('Videos: use the \u2913 download button'); return; }
            copyImage(lbName).then((ok) => {
                b.textContent = ok ? '\u2713' : '\u2715';
                setTimeout(() => { b.textContent = '\u{1F4CB}'; }, 900);
            });
        });
        lbEl.querySelector('.lb-down').addEventListener('click', () => {
            const src = lbSrcFor(lbName);
            if (!src) return;
            const a = document.createElement('a');
            a.href = src;
            a.download = lbName;
            document.body.appendChild(a);
            a.click();
            a.remove();
        });
        lbEl.querySelector('.lb-del').addEventListener('click', lbDelete);
        // THE size authority: pure arithmetic, immune to any CSS.
        // scale = min(available/natural) → whole image, as big as fits.
        lbEl.querySelector('.lb-img').addEventListener('load', lbFit);
        lbEl.querySelector('.lb-vid').addEventListener('loadedmetadata', lbFit);
        window.addEventListener('resize', () => { if (previewOpen()) lbFit(); });
        // touch: swipe left/right browses, swipe down closes — ONLY when not zoomed
        let lbTouch = null;
        lbEl.addEventListener('touchstart', (e) => {
            const t = e.touches[0];
            lbTouch = { x: t.clientX, y: t.clientY };
        }, { passive: true });
        lbEl.addEventListener('touchend', (e) => {
            if (!lbTouch) return;
            const t = e.changedTouches[0];
            const dx = t.clientX - lbTouch.x, dy = t.clientY - lbTouch.y;
            lbTouch = null;
            if (lbZoom.z > 1.01) return;               // zoomed: fingers pan, not browse
            if (e.target.closest && e.target.closest('.lb-strip, .lb-actions, .lb-nav')) return;
            if (Math.abs(dx) > 48 && Math.abs(dy) < 80) lbNav(dx < 0 ? 1 : -1);
            else if (dy > 70 && Math.abs(dx) < 80) closeLightbox();
        }, { passive: true });

        // ---- v4.9 fullscreen ZOOM (images): wheel / double-click / drag-pan on
        //      desktop, pinch / one-finger-pan / double-tap on touch. The <img>
        //      holds the FULL-RES file, so zooming stays sharp. Videos excluded
        //      (their controls need the gestures). ----
        const zImg = lbEl.querySelector('.lb-img');
        function zApply() {
            zImg.style.transformOrigin = 'center center';
            zImg.style.transform = lbZoom.z > 1
                ? 'translate(' + lbZoom.tx + 'px,' + lbZoom.ty + 'px) scale(' + lbZoom.z + ')'
                : '';
            zImg.style.cursor = lbZoom.z > 1 ? 'grab' : '';
        }
        function zClamp() {
            const r = zImg.getBoundingClientRect();     // includes the current transform
            const mx = Math.max(0, (r.width - window.innerWidth) / 2) + 60;
            const my = Math.max(0, (r.height - window.innerHeight) / 2) + 60;
            lbZoom.tx = Math.max(-mx, Math.min(mx, lbZoom.tx));
            lbZoom.ty = Math.max(-my, Math.min(my, lbZoom.ty));
        }
        function zReset() { lbZoom.z = 1; lbZoom.tx = 0; lbZoom.ty = 0; zApply(); }
        function zAt(cx, cy, f) {                       // zoom by factor f toward a screen point
            const nz = Math.max(1, Math.min(12, lbZoom.z * f));
            f = nz / lbZoom.z;
            if (f === 1) return;
            const r = zImg.getBoundingClientRect();
            lbZoom.tx += (cx - (r.left + r.width / 2)) * (1 - f);
            lbZoom.ty += (cy - (r.top + r.height / 2)) * (1 - f);
            lbZoom.z = nz;
            if (lbZoom.z <= 1.001) { lbZoom.z = 1; lbZoom.tx = 0; lbZoom.ty = 0; }
            zClamp();
            zApply();
        }
        lbEl.addEventListener('wheel', (e) => {
            if (isVid(lbName)) return;
            if (e.target.closest && e.target.closest('.lb-strip, .lb-actions')) return;
            e.preventDefault();
            zAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.25 : 0.8);
        }, { passive: false });
        zImg.addEventListener('dblclick', (e) => {
            e.preventDefault();
            e.stopPropagation();
            if (lbZoom.z > 1) zReset(); else zAt(e.clientX, e.clientY, 2.5);
        });
        let zDrag = null;
        zImg.addEventListener('mousedown', (e) => {
            if (lbZoom.z <= 1 || e.button !== 0) return;
            e.preventDefault();
            zDrag = { x: e.clientX, y: e.clientY };
            zImg.style.cursor = 'grabbing';
        });
        window.addEventListener('mousemove', (e) => {
            if (!zDrag) return;
            lbZoom.tx += e.clientX - zDrag.x;
            lbZoom.ty += e.clientY - zDrag.y;
            zDrag = { x: e.clientX, y: e.clientY };
            zClamp();
            zApply();
        });
        window.addEventListener('mouseup', () => { if (zDrag) { zDrag = null; zApply(); } });
        let zPinch = null, zPan = null, zLastTap = 0, zLastPos = null;
        lbEl.addEventListener('touchstart', (e) => {
            if (isVid(lbName)) return;
            if (e.touches.length === 2) {
                const a = e.touches[0], b = e.touches[1];
                zPinch = { d: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) };
                zPan = null;
                lbTouch = null;                          // a pinch is never a swipe
            } else if (e.touches.length === 1 && lbZoom.z > 1) {
                zPan = { x: e.touches[0].clientX, y: e.touches[0].clientY };
            }
        }, { passive: true });
        lbEl.addEventListener('touchmove', (e) => {
            if (isVid(lbName)) return;
            if (zPinch && e.touches.length === 2) {
                e.preventDefault();
                const a = e.touches[0], b = e.touches[1];
                const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
                if (zPinch.d > 0) zAt((a.clientX + b.clientX) / 2, (a.clientY + b.clientY) / 2, d / zPinch.d);
                zPinch.d = d;
            } else if (zPan && e.touches.length === 1) {
                e.preventDefault();
                const t = e.touches[0];
                lbZoom.tx += t.clientX - zPan.x;
                lbZoom.ty += t.clientY - zPan.y;
                zPan = { x: t.clientX, y: t.clientY };
                zClamp();
                zApply();
            }
        }, { passive: false });
        lbEl.addEventListener('touchend', (e) => {
            if (e.touches.length < 2) zPinch = null;
            if (e.touches.length < 1) zPan = null;
            if (isVid(lbName)) return;
            // double-tap toggles zoom
            if (e.changedTouches.length === 1 && !e.touches.length) {
                const t = e.changedTouches[0];
                const now = Date.now();
                if (now - zLastTap < 320 && zLastPos &&
                    Math.hypot(t.clientX - zLastPos.x, t.clientY - zLastPos.y) < 40 &&
                    !(e.target.closest && e.target.closest('.lb-strip, .lb-actions, .lb-nav'))) {
                    if (lbZoom.z > 1) zReset(); else zAt(t.clientX, t.clientY, 2.5);
                    zLastTap = 0;
                    return;
                }
                zLastTap = now;
                zLastPos = { x: t.clientX, y: t.clientY };
            }
        }, { passive: true });
    }
    function lbFit() {
        if (!lbEl) return;
        const v = isVid(lbName);
        const img = lbEl.querySelector(v ? '.lb-vid' : '.lb-img');
        const nw = v ? img.videoWidth : img.naturalWidth;
        const nh = v ? img.videoHeight : img.naturalHeight;
        if (!nw || !nh) return;
        const mob = window.innerWidth < 700;
        const availW = Math.max(window.innerWidth - (mob ? 12 : 32), 100);
        const availH = Math.max(window.innerHeight - (mob ? 116 : 136), 100);
        const s = Math.min(availW / nw, availH / nh);
        const w = Math.round(nw * s), h = Math.round(nh * s);
        img.style.setProperty('width', w + 'px', 'important');
        img.style.setProperty('height', h + 'px', 'important');
        img.style.setProperty('max-width', 'none', 'important');
        img.style.setProperty('max-height', 'none', 'important');
        img.style.setProperty('object-fit', 'fill', 'important');  // box == bitmap exactly
    }
    function lbFlash(txt) {
        if (!lbEl) return;
        lbEl.querySelector('.lb-name').textContent = txt;
        clearTimeout(lbFlashTimer);
        lbFlashTimer = setTimeout(lbRender, 1200);
    }
    function lbRender() {
        if (!lbEl || !lbName) return;
        const names = lbNames();
        const i = names.indexOf(lbName);
        const src = lbSrcFor(lbName);
        const img = lbEl.querySelector('.lb-img');
        // v4.9: a different item resets the zoom (repaints of the SAME item keep it)
        if (lbZoom.name !== lbName) {
            lbZoom.name = lbName;
            lbZoom.z = 1; lbZoom.tx = 0; lbZoom.ty = 0;
            img.style.transform = '';
            img.style.cursor = '';
        }
        const vidEl = lbEl.querySelector('.lb-vid');
        const v = isVid(lbName);
        img.style.display = v ? 'none' : '';
        vidEl.style.display = v ? '' : 'none';
        if (v) {
            if (src && vidEl.src !== src) vidEl.src = src;
            if (vidEl.videoWidth) lbFit();
        } else {
            if (vidEl.src) { vidEl.pause(); vidEl.removeAttribute('src'); vidEl.load(); }
            if (src && img.src !== src) img.src = src;
            if (img.complete && img.naturalWidth) lbFit();
        }
        lbEl.querySelector('.lb-name').textContent =
            lbName + (names.length > 1 ? '  \u00B7  ' + (i + 1) + ' / ' + names.length : '');
        const multi = names.length > 1;
        lbEl.querySelector('.lb-nav.prev').style.display = multi ? '' : 'none';
        lbEl.querySelector('.lb-nav.next').style.display = multi ? '' : 'none';
        const strip = lbEl.querySelector('.lb-strip');
        strip.style.display = multi ? '' : 'none';
        if (multi) {
            const key = names.join('|');
            if (strip.dataset.key !== key) {
                strip.dataset.key = key;
                strip.textContent = '';
                names.forEach((n) => {
                    const im = document.createElement(isVid(n) ? 'video' : 'img');
                    if (isVid(n)) { im.muted = true; im.preload = 'metadata'; }
                    im.src = lbSrcFor(n) || '';
                    im.title = n;
                    im.addEventListener('click', () => { lbName = n; lbRender(); });
                    strip.appendChild(im);
                });
            }
            let cur = null;
            $$('img, video', strip).forEach((im) => {
                const on = im.title === lbName;
                im.classList.toggle('current', on);
                if (on) cur = im;
            });
            if (cur && cur.scrollIntoView) {
                cur.scrollIntoView({ block: 'nearest', inline: 'center', behavior: 'smooth' });
            }
        }
    }
    function openLightbox(name) {
        if (!name) return;
        if (!lbEl) buildLightbox();
        lbName = name;
        if (!lbEl.isConnected) document.body.appendChild(lbEl);
        lbRender();
    }
    function closeLightbox() {
        if (!lbEl) return;
        const v = lbEl.querySelector('.lb-vid');
        if (v && v.pause) v.pause();
        lbEl.remove();
    }
    function lbNav(d) {
        const names = lbNames();
        if (!names.length) return;
        const i = names.indexOf(lbName);
        lbName = names[(i + d + names.length) % names.length];
        lbRender();
    }
    function lbDelete() {
        if (!lbName) return;
        const names = lbNames();
        const i = names.indexOf(lbName);
        const next = names[i + 1] || names[i - 1] || null;
        picked.clear();
        picked.add(lbName);
        syncStore();
        clickDeleteBtn();
        if (next) { lbName = next; lbRender(); } else { closeLightbox(); }
    }
    function lbSync() {
        if (!lbEl || !lbEl.isConnected) return;
        const names = lbNames();
        if (!names.length) { closeLightbox(); return; }
        if (!names.includes(lbName)) lbName = names[0];
        lbRender();
    }

    // ---- Flow-style hover actions on each thumbnail (reuse / ingredient / delete) ----
    const TRASH_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"'
        + ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/>'
        + '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/>'
        + '<path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>'
        + '<path d="M10 11v6"/><path d="M14 11v6"/></svg>';
    const actions = document.createElement('div');
    actions.className = 'thumb-actions';
    let hoverThumb = null;
    function mkBtn(txt, title, fn, cls) {
        const b = document.createElement('button');
        if (txt.startsWith('<svg')) b.innerHTML = txt; else b.textContent = txt;
        b.title = title;
        if (cls) b.classList.add(cls);
        b.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            const name = hoverThumb && thumbName(hoverThumb);
            if (name) fn(name, b);
        }, true);
        actions.appendChild(b);
    }
    mkBtn('↻', 'Reuse this item’s prompt & settings',
          (n) => storeThenClick('reuse-store', { n: n }, 'reuse-btn'));
    mkBtn('+', 'Add to ingredients (images only)',
          (n) => storeThenClick('ingredient-store', { n: n }, 'ingredient-btn'), 'act-add');
    mkBtn('📋', 'Copy to clipboard (images only)', (n, b) => {
        if (isVid(n)) {
            b.textContent = '✕';
            setTimeout(() => { b.textContent = '📋'; }, 900);
            return;
        }
        copyImage(n).then((ok) => {
            b.textContent = ok ? '✓' : '✕';
            setTimeout(() => { b.textContent = '📋'; }, 900);
        });
    });
    mkBtn(TRASH_SVG, 'Delete this image', (n) => {
        picked.clear();
        picked.add(n);
        syncStore();
        paint();
        clickDeleteBtn();
    });
    document.addEventListener('mouseover', (e) => {
        if (e.target.closest && e.target.closest('.thumb-actions')) return;
        const g = gridWrap();
        const t = e.target.closest ? e.target.closest('.thumbnail-item') : null;
        if (!g || !t || !g.contains(t) || previewOpen()) {
            if (actions.isConnected && !(t && actions.parentElement === t)) actions.remove();
            hoverThumb = null;
            return;
        }
        if (hoverThumb !== t) {
            hoverThumb = t;
            // v4.0: videos never offer "+ make ingredient" — CSS hides .act-add
            actions.classList.toggle('is-vid', isVid(thumbName(t) || ''));
            t.appendChild(actions);
        }
    }, true);

    // ---- ingredient tray: hover actions (insert @tag / remove one) ----
    const trayActions = document.createElement('div');
    trayActions.className = 'thumb-actions';
    let trayHover = null;
    function trayThumbs() {
        const g = document.getElementById('ingredient-tray');
        return g ? $$('.thumbnail-item', g) : [];
    }
    function mkTrayBtn(txt, title, fn) {
        const b = document.createElement('button');
        b.textContent = txt;
        b.title = title;
        b.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            const i = trayHover ? trayThumbs().indexOf(trayHover) : -1;
            if (i >= 0) fn(i, b);
        }, true);
        trayActions.appendChild(b);
    }
    function insertTag(n) {
        const host = $('#prompt-box');
        const box = host && (host.querySelector('textarea') || host.querySelector('input'));
        if (!box) return;
        const s = box.selectionStart != null ? box.selectionStart : box.value.length;
        const e2 = box.selectionEnd != null ? box.selectionEnd : s;
        const before = box.value.slice(0, s);
        const after = box.value.slice(e2);
        const pad = before && !/\s$/.test(before) ? ' ' : '';
        const ins = pad + '@' + n + ' ';
        setNative(box, before + ins + after);
        box.focus();
        const pos = before.length + ins.length;
        try { box.setSelectionRange(pos, pos); } catch (err) {}
    }
    mkTrayBtn('@', 'Put this ingredient’s @tag into the prompt',
              (i) => insertTag(i + 1));
    mkTrayBtn('✖', 'Remove this ingredient from the tray',
              (i) => storeThenClick('tray-remove-store', { i: i }, 'tray-remove-btn'));
    document.addEventListener('mouseover', (e) => {
        if (e.target.closest && e.target.closest('.thumb-actions')) return;
        const g = document.getElementById('ingredient-tray');
        const t = e.target.closest ? e.target.closest('.thumbnail-item') : null;
        if (!g || !t || !g.contains(t)) {
            if (trayActions.isConnected && !(t && trayActions.parentElement === t)) trayActions.remove();
            trayHover = null;
            return;
        }
        if (trayHover !== t) {
            trayHover = t;
            t.appendChild(trayActions);
        }
    }, true);

    // ---- Ctrl+V anywhere / drag & drop: image goes straight into the tray.
    // The browser uploads through Gradio's own upload API, then hands the temp
    // path to Python over the ghost-button bridge (UploadButton's hidden input
    // can't be fed programmatically in 6.20). Adds are serialized so several
    // dropped files never race the single store textbox. ----
    async function uploadToTray(file) {
        try {
            const fd = new FormData();
            const name = (file.name && /\.[a-z0-9]{2,5}$/i.test(file.name))
                ? file.name : 'pasted_' + Date.now() + '.png';
            fd.append('files', file, name);
            const r = await fetch('/gradio_api/upload', { method: 'POST', body: fd });
            const paths = await r.json();
            if (Array.isArray(paths) && paths[0]) {
                storeThenClick('paste-store', { p: paths[0] }, 'paste-btn');
                return true;
            }
        } catch (e) { console.warn('image upload failed', e); }
        return false;
    }
    let trayChain = Promise.resolve();
    function queueTrayAdd(file) {
        trayChain = trayChain
            .then(() => uploadToTray(file))
            .then((ok) => new Promise((res) => setTimeout(() => res(ok), 250)));
        return trayChain;
    }
    let pasteHandled = 0;   // timestamp of the last real `paste` event (see Ctrl+V below)
    document.addEventListener('paste', (e) => {
        const cd = e.clipboardData;
        if (!cd) return;
        let img = null;
        for (const it of cd.items || []) {
            if (it.kind === 'file' && it.type && it.type.startsWith('image/')) {
                img = it.getAsFile();
                break;
            }
        }
        if (!img) return;
        const t = e.target;
        const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
        if (typing && cd.getData && cd.getData('text')) return;  // pasting text into a field wins
        e.preventDefault();
        pasteHandled = Date.now();
        queueTrayAdd(img).then((ok) => {
            closePasteSheet(ok);
            // Python decides where it lands (Start/End frame in Frames mode, else tray)
            flashPill(ok ? 'Image pasted \u2713' : 'Paste failed \u2715');
        });
    });
    // v6.1: Ctrl/Cmd+V must work when NOTHING is focused too. With no editable
    // element focused some browsers never fire a `paste` event at all, so the
    // handler above never runs. Wait one tick \u2014 if no paste event arrived, read the
    // clipboard ourselves through the same pipeline (\u2192 Start frame in Frames mode).
    document.addEventListener('keydown', (e) => {
        if (!(e.ctrlKey || e.metaKey) || e.key.toLowerCase() !== 'v') return;
        const t = e.target;
        if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
        const at = Date.now();
        setTimeout(() => {
            if (pasteHandled >= at) return;    // the paste event already did it
            pasteFromClipboard();
        }, 120);
    });
    document.addEventListener('dragover', (e) => {
        if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes('Files')) {
            e.preventDefault();
        }
    });
    document.addEventListener('drop', (e) => {
        const files = e.dataTransfer && e.dataTransfer.files;
        if (!files || !files.length) return;
        const imgs = Array.from(files).filter((f) => f.type && f.type.startsWith('image/'));
        if (!imgs.length) return;
        e.preventDefault();
        Promise.all(imgs.map(queueTrayAdd)).then((rs) => {
            const n = rs.filter(Boolean).length;
            if (n) flashPill(n + ' image' + (n > 1 ? 's' : '') + ' added to ingredients \u2713');
        });
    });

    // ---- v5.0 mobile paste: phones have no Ctrl+V, so LONG-PRESS the \uff0b button
    //      reads an image out of the clipboard (async Clipboard API) and drops it
    //      into the ingredient tray. Normal tap still opens the photo picker. This
    //      keeps the composer pill pixel-identical (Gavin's ask). ----
    // v6.0 PASTE SHEET \u2014 the dependable mobile path. iOS refuses
    // navigator.clipboard.read() on many setups, but the NATIVE paste menu into a
    // focused field always works, and our global 'paste' handler above catches the
    // image. So when the API route is unavailable we pop a tiny focused field and
    // tell the user exactly what to do. Python then routes the image (Start/End
    // frame in Frames mode, otherwise the ingredient tray).
    let pasteSheet = null;
    function closePasteSheet(ok) {
        if (!pasteSheet) return;
        pasteSheet.remove();
        pasteSheet = null;
        if (ok) flashPill('Pasted \u2713');
    }
    function openPasteSheet() {
        closePasteSheet(false);
        pasteSheet = document.createElement('div');
        pasteSheet.id = 'paste-sheet';
        pasteSheet.innerHTML =
            '<div class="ps-card">'
            + '<div class="ps-title">Paste an image</div>'
            + '<input class="ps-input" type="text" placeholder="Long-press here \u2192 Paste" />'
            + '<div class="ps-hint">Long-press the box above, then tap <b>Paste</b>.</div>'
            + '<button class="ps-cancel" type="button">Cancel</button>'
            + '</div>';
        pasteSheet.addEventListener('click', (e) => {
            if (e.target === pasteSheet || e.target.closest('.ps-cancel')) closePasteSheet(false);
        });
        document.body.appendChild(pasteSheet);
        const inp = pasteSheet.querySelector('.ps-input');
        // a text paste means they copied text, not an image \u2014 say so instead of hanging
        inp.addEventListener('input', () => {
            if (inp.value.trim()) {
                inp.value = '';
                flashPill('That was text \u2014 copy an image first');
            }
        });
        setTimeout(() => { try { inp.focus(); } catch (e) {} }, 30);
    }
    // v5.1: MUST run inside the touch gesture (see the touchend handler below) \u2014
    // iOS rejects clipboard.read() called from a deferred setTimeout.
    async function pasteFromClipboard() {
        if (!navigator.clipboard || !navigator.clipboard.read) {
            openPasteSheet();
            return;
        }
        try {
            const items = await navigator.clipboard.read();
            for (const it of items) {
                const type = (it.types || []).find((t) => t.startsWith('image/'));
                if (!type) continue;
                const blob = await it.getType(type);
                const ext = (type.split('/')[1] || 'png').replace('jpeg', 'jpg');
                const file = new File([blob], 'pasted_' + Date.now() + '.' + ext, { type });
                const ok = await queueTrayAdd(file);
                flashPill(ok ? 'Pasted \u2713' : 'Paste failed \u2715');
                return;
            }
            flashPill('No image in your clipboard \u2014 copy one first');
        } catch (err) {
            // iOS refuses read() on most setups \u2014 fall back to the native paste menu
            openPasteSheet();
        }
    }
    if (isTouch) {
        let addStart = 0, addMoved = false, addHapticTimer = null;
        let addStartX = 0, addStartY = 0;
        let addLpFired = false, addLpFiredAt = 0;
        document.addEventListener('touchstart', (e) => {
            const b = e.target.closest && e.target.closest('#add-files-btn');
            if (!b) return;
            b.setAttribute('title', 'Tap: add photos \u00b7 Hold: paste');
            addStart = Date.now();
            addMoved = false;
            const tp = e.touches && e.touches[0];
            addStartX = tp ? tp.clientX : 0;
            addStartY = tp ? tp.clientY : 0;
            clearTimeout(addHapticTimer);
            addHapticTimer = setTimeout(() => {
                if (!addMoved && navigator.vibrate) navigator.vibrate(15);   // haptic only
            }, 300);
        }, { passive: true, capture: true });
        document.addEventListener('touchmove', (e) => {
            if (!(e.target.closest && e.target.closest('#add-files-btn'))) return;
            // v5.2: only a REAL drag/scroll (>12px) cancels the hold \u2014 a finger held
            // still always jitters a few px, and that was silently killing the paste
            const tp = e.touches && e.touches[0];
            if (tp && (Math.abs(tp.clientX - addStartX) > 12 || Math.abs(tp.clientY - addStartY) > 12)) {
                addMoved = true;
            }
        }, { passive: true, capture: true });
        document.addEventListener('touchcancel', () => { clearTimeout(addHapticTimer); },
                                  { passive: true, capture: true });
        // the KEY fix: fire the clipboard read from THIS gesture (not a setTimeout),
        // so iOS still counts it as user-activated
        document.addEventListener('touchend', (e) => {
            if (!(e.target.closest && e.target.closest('#add-files-btn'))) return;
            clearTimeout(addHapticTimer);
            if (!addMoved && Date.now() - addStart >= 300) {
                addLpFired = true;
                addLpFiredAt = Date.now();
                e.preventDefault();   // stop this hold from becoming a picker-opening click
                pasteFromClipboard();
            }
        }, { capture: true });        // NOT passive \u2014 we preventDefault above
        // belt-and-braces: eat any click a long-press still leaves behind
        document.addEventListener('click', (e) => {
            if (!(e.target.closest && e.target.closest('#add-files-btn'))) return;
            if (addLpFired && Date.now() - addLpFiredAt < 700) {
                addLpFired = false;
                e.stopPropagation();
                e.preventDefault();
            }
        }, true);
        // one-time discoverability nudge (no persistent UI): first mobile open only
        try {
            if (!localStorage.getItem('bz_paste_hint')) {
                localStorage.setItem('bz_paste_hint', '1');
                setTimeout(() => flashPill('Tip: hold \uff0b to paste from clipboard'), 1400);
            }
        } catch (e) {}
    }

    // ---- touch (v4.9): long-press a tile to reveal its ACTION buttons
    //      (reuse / +ingredient / copy / delete) — selecting moved to the
    //      "Select" chip. lpFired still eats the click so no lightbox opens. ----
    document.addEventListener('touchstart', (e) => {
        // v5.0: in Select mode a tap must simply toggle the tile — never pop the
        // action cluster (that fight was what made multi-select feel finicky)
        if (selMode) return;
        const g = gridWrap();
        const t = e.target.closest && e.target.closest('.thumbnail-item');
        if (!g || !t || !g.contains(t) || previewOpen()) return;
        clearTimeout(lpTimer);
        lpTimer = setTimeout(() => {
            const n = thumbName(t);
            if (!n) return;
            lpFired = true;
            lpFiredAt = Date.now();
            hoverThumb = t;
            actions.classList.toggle('is-vid', isVid(n));
            t.appendChild(actions);
            actions.classList.add('show');
            if (navigator.vibrate) navigator.vibrate(15);
        }, 300);   // v5.0: 450 → 300ms, snappier but still above scroll-intent
    }, { passive: true, capture: true });
    ['touchend', 'touchmove', 'touchcancel'].forEach((ev) =>
        document.addEventListener(ev, () => clearTimeout(lpTimer), { passive: true, capture: true }));
    if (isTouch) {
        // long-press must show OUR actions, never the browser/iOS image menu
        // (grid, ingredient tray, and the fullscreen viewer alike)
        document.addEventListener('contextmenu', (e) => {
            if (e.target.closest &&
                e.target.closest('#result-gallery .thumbnail-item, #ingredient-tray .thumbnail-item, #nb-lightbox')) {
                e.preventDefault();
            }
        });
    }

    // ---- touch: tap an ingredient to show its @ / remove buttons (no hover on phones) ----
    document.addEventListener('click', (e) => {
        if (!isTouch) return;
        if (e.target.closest && e.target.closest('.thumb-actions')) return;
        const g = document.getElementById('ingredient-tray');
        const t = e.target.closest ? e.target.closest('.thumbnail-item') : null;
        if (g && t && g.contains(t)) {
            e.stopPropagation();
            e.preventDefault();
            trayHover = t;
            t.appendChild(trayActions);
            trayActions.classList.add('show');
        } else if (trayActions.isConnected) {
            trayActions.classList.remove('show');
        }
    }, true);

    function clickGenerate() {
        // v6.1a: bail on an empty prompt right here. The server raises the same
        // "Write a prompt first" error, but that logs a full traceback in the
        // console for what is really just a typo — and now that plain Enter
        // generates, empty-Enter happens a lot. Same message, instantly, no
        // round-trip. (Every mode requires a prompt, so this mirrors the server.)
        const ta = document.querySelector('#prompt-box textarea');
        if (ta && !ta.value.trim()) {
            flashPill('Write a prompt first');
            ta.focus();
            return false;
        }
        const host = $('#go-btn');
        const btn = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        if (btn) btn.click();
        return !!btn;
    }
    // ---- Enter = Generate (v6.1) ----
    //  · Ctrl/Cmd+Enter works from ANYWHERE on the page (was prompt-only).
    //  · A bare Enter while the prompt is focused also generates; Shift+Enter still
    //    inserts a newline so multi-line prompts remain possible.
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Enter' || previewOpen()) return;
        const inPrompt = !!(e.target.closest && e.target.closest('#prompt-box'));
        if (e.ctrlKey || e.metaKey) {          // anywhere
            e.preventDefault();
            clickGenerate();
            return;
        }
        if (inPrompt && !e.shiftKey && !e.altKey) {
            e.preventDefault();                // plain Enter in the prompt = go
            clickGenerate();
        }
    }, true);

    // ---- Flow chrome: composer model-pill + settings pop-up, canvas View panel.
    // Everything below is pure client-side sugar over the existing Gradio controls
    // (no re-wiring) — the model dropdowns and grid/filter still drive Python. ----
    const VIEW_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"'
        + ' stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        + '<line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/>'
        + '<line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/>'
        + '<line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/>'
        + '<line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/>'
        + '<line x1="18" x2="22" y1="16" y2="16"/></svg>';

    function ddValue(id) {
        const host = document.getElementById(id);
        const inp = host && host.querySelector('input');
        return inp ? inp.value : null;
    }
    function visibleModelLabel() {
        // Gradio hides the inactive model dropdown with display:none → offsetParent
        // is null for it; the visible one measures normally.
        const img = document.getElementById('model-img');
        const vid = document.getElementById('model-vid');
        if (vid && vid.offsetParent !== null) return ddValue('model-vid');
        if (img && img.offsetParent !== null) return ddValue('model-img');
        return ddValue('model-img') || ddValue('model-vid');
    }
    function ensureModelPill() {
        const pillRow = document.getElementById('pill-row');
        if (!pillRow) return null;
        let pill = document.getElementById('model-pill');
        if (!pill) {
            pill = document.createElement('button');
            pill.id = 'model-pill';
            pill.type = 'button';
            pill.title = 'Model & settings';
            pill.innerHTML = '<span class="mp-text"></span><span class="mp-caret">▲</span>';
            pill.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                const c = document.getElementById('composer');
                if (c) c.classList.toggle('pop-open');
            });
        }
        const go = document.getElementById('go-btn');
        if (go && go.parentElement) {
            if (pill.parentElement !== go.parentElement || pill.nextElementSibling !== go) {
                go.parentElement.insertBefore(pill, go);
            }
        } else if (pill.parentElement !== pillRow) {
            pillRow.appendChild(pill);
        }
        return pill;
    }
    function updateModelPill() {
        const pill = ensureModelPill();
        if (!pill) return;
        let model = (visibleModelLabel() || 'Model').replace(/\s*·\s*video$/i, '');
        const count = ddValue('count-dd');
        const label = model + (count ? '  ·  x' + count : '');
        const txt = pill.querySelector('.mp-text');
        if (txt && txt.textContent !== label) txt.textContent = label;
        // v6.0: mirror the pre-flight cost (Python owns the prices) into the pill so
        // you can see the spend before generating without opening settings
        const src = document.querySelector('#cost-estimate b');
        let cost = pill.querySelector('.mp-cost');
        if (src && src.textContent) {
            if (!cost) {
                cost = document.createElement('span');
                cost.className = 'mp-cost';
                const caret = pill.querySelector('.mp-caret');
                pill.insertBefore(cost, caret || null);
            }
            const t = '·  ' + src.textContent.trim();
            if (cost.textContent !== t) cost.textContent = t;
        } else if (cost) {
            cost.remove();
        }
    }
    function ensureViewToggle() {
        const card = document.getElementById('canvas-card');
        const bar = document.getElementById('topbar');
        if (!card || !bar) return;
        let btn = document.getElementById('view-toggle');
        if (!btn) {
            btn = document.createElement('button');
            btn.id = 'view-toggle';
            btn.type = 'button';
            btn.title = 'View options — grid size & filter';
            btn.innerHTML = VIEW_SVG;
            btn.addEventListener('click', (e) => {
                e.preventDefault();
                e.stopPropagation();
                card.classList.toggle('view-open');
            });
        }
        // live in the top bar (next to Settings) so it never covers the grid's
        // top-right image or its hover actions
        const settings = document.getElementById('settings-open-btn');
        if (settings && settings.parentElement === bar) {
            if (btn.nextElementSibling !== settings) bar.insertBefore(btn, settings);
        } else if (btn.parentElement !== bar) {
            bar.appendChild(btn);
        }
    }
    function syncVmodeFrames() {
        // Ingredients mode must never show the Start/End frame slots — drive it
        // straight off the vmode radio so it's correct no matter what Gradio does.
        const vr = document.getElementById('video-row');
        if (!vr) return;
        const checked = document.querySelector('#vmode-radio input:checked');
        vr.classList.toggle('ingredients-mode', !!(checked && checked.value === 'ingredients'));
    }
    // v6.0: a 📋 button in each frame slot (touch only) opens the paste sheet —
    // the pasted image is routed to Start/End by Python's route_added_images().
    function ensureFramePasteBtns() {
        if (!isTouch) return;
        ['frame-start', 'frame-end'].forEach((id) => {
            const slot = document.getElementById(id);
            if (!slot || slot.querySelector('.frame-paste-btn')) return;
            const b = document.createElement('button');
            b.className = 'frame-paste-btn';
            b.type = 'button';
            b.textContent = '📋';
            b.title = 'Paste an image into this slot';
            b.addEventListener('click', (e) => {
                e.stopPropagation();
                e.preventDefault();
                pasteFromClipboard();
            });
            slot.appendChild(b);
        });
    }
    // v6.0: small ✕ so the settings sheet can be dismissed on a phone
    function ensureSettingsClose() {
        if (!isTouch) return;
        const pop = document.getElementById('settings-pop');
        if (!pop || document.getElementById('settings-pop-close')) return;
        const x = document.createElement('button');
        x.id = 'settings-pop-close';
        x.type = 'button';
        x.textContent = '✕';
        x.title = 'Close settings';
        x.addEventListener('click', (e) => {
            e.stopPropagation();
            e.preventDefault();
            const c = document.getElementById('composer');
            if (c) c.classList.remove('pop-open');
        });
        pop.insertBefore(x, pop.firstChild);
    }
    // v6.0 had a syncConfirmCards() here that toggled a .bz-empty class to
    // collapse a confirm card whose button row was hidden. v6.4 removed it:
    // measured in Gradio 6.20, a visible=False Group is not rendered into the
    // DOM AT ALL, so there is no leftover "empty strip" for it to collapse —
    // it was dead code guarding a problem that no longer exists.
    // v6.0: colour the Save / Test-connection panel by its outcome marker
    function syncKeyStatus() {
        const el = document.getElementById('key-status');
        if (!el) return;
        const txt = (el.innerText || '').trim();
        const cls = !txt ? 'blank'
            : txt.startsWith('✅') ? 'ok'
            : txt.startsWith('⚠️') ? 'warn'
            : txt.startsWith('❌') ? 'bad' : '';
        ['ok', 'warn', 'bad', 'blank'].forEach((c) => el.classList.toggle(c, c === cls));
    }
    function setupFlowChrome() {
        updateModelPill();
        ensureViewToggle();
        syncVmodeFrames();
        ensureFramePasteBtns();
        ensureSettingsClose();
        syncKeyStatus();
    }
    // click outside closes the settings pop-up / View panel (menus keep them open)
    document.addEventListener('click', (e) => {
        const inMenu = e.target.closest && e.target.closest('ul.options');
        const composer = document.getElementById('composer');
        if (composer && composer.classList.contains('pop-open')) {
            const pop = document.getElementById('settings-pop');
            const pill = document.getElementById('model-pill');
            const keep = inMenu || (pop && pop.contains(e.target)) || (pill && pill.contains(e.target));
            if (!keep) composer.classList.remove('pop-open');
        }
        const card = document.getElementById('canvas-card');
        if (card && card.classList.contains('view-open')) {
            const panel = document.getElementById('view-panel');
            const vt = document.getElementById('view-toggle');
            const keep = inMenu || (panel && panel.contains(e.target)) || (vt && vt.contains(e.target));
            if (!keep) card.classList.remove('view-open');
        }
    });
    // Esc closes either floating panel (works even while focused in the prompt)
    document.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        const composer = document.getElementById('composer');
        const card = document.getElementById('canvas-card');
        if (composer) composer.classList.remove('pop-open');
        if (card) card.classList.remove('view-open');
    });

    // ============================================================
    //  v6.1 DESKTOP SHORTCUTS — one family: Alt + a key that IS the word.
    //  Alt was chosen because every useful Ctrl+letter is taken by the browser
    //  (Ctrl+S/P/D/L/E/K/O/U/J/H) and because Alt+key types nothing, so these
    //  keep working while you're in the middle of writing a prompt.
    //    Alt+S  settings pop-up      Alt+`  focus / unfocus the prompt
    //    Alt+L  video Length         Alt+C  clear all   (Alt+Z puts the prompt back)
    //  Aliases: Ctrl+,  = Alt+S (universal "preferences");  /  focuses the prompt
    //  when you aren't already typing (YouTube/GitHub/Slack convention).
    // ============================================================

    // v6.2 — SOLO-ALT GUARD (must come first, and must NOT sit behind the
    // previewOpen() guard below, so it is active everywhere).
    // Chrome/Edge on Windows hand focus to the browser's own menu button when Alt
    // is pressed and released with no other key. Focus leaves the document, the
    // page goes deaf, and none of the shortcuts work until you click back in.
    // Swallowing the bare Alt keypress stops the browser ever treating it that way.
    // Alt+<key> combos are untouched: the letter is its own keydown, and any other
    // keydown clears the flag so genuine browser combos (Alt+←, Alt+D, Alt+F) and
    // Windows Alt+numpad character codes still behave normally.
    let altSolo = false;
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Alt') { altSolo = true; e.preventDefault(); }
        else altSolo = false;
    }, true);
    document.addEventListener('keyup', (e) => {
        if (e.key !== 'Alt') return;
        if (altSolo) e.preventDefault();
        altSolo = false;
    }, true);

    let clearedPrompt = null;          // Alt+C snapshot, restored by Alt+Z
    function promptBox() { return document.querySelector('#prompt-box textarea'); }
    function isTypingIn(t) {
        return !!(t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable));
    }
    function toggleSettingsPop(force) {
        const c = document.getElementById('composer');
        if (!c) return false;
        const open = force === undefined ? !c.classList.contains('pop-open') : force;
        c.classList.toggle('pop-open', open);
        return open;
    }
    function focusPrompt(toggle) {
        const ta = promptBox();
        if (!ta) return;
        if (toggle && document.activeElement === ta) { ta.blur(); return; }
        ta.focus();
        // caret to the end so you continue writing rather than overwrite
        try { ta.setSelectionRange(ta.value.length, ta.value.length); } catch (err) {}
    }
    function openSecondsDropdown() {
        // the Seconds control lives inside the settings pop, so open that first
        const vidOn = !!document.querySelector('#settings-pop input[value="video"]:checked')
            || !!document.getElementById('model-vid');
        toggleSettingsPop(true);
        setTimeout(() => {
            const blocks = $$('#settings-pop .block');
            const sec = blocks.find((b) => /Seconds/.test(b.textContent) && b.offsetParent !== null);
            if (!sec) {
                flashPill('Seconds is video-only — switch to 🎬 Video first');
                return;
            }
            const inp = sec.querySelector('input');
            if (inp) { inp.focus(); inp.click(); }
        }, 90);
        return vidOn;
    }
    function clearAllViaShortcut() {
        const ta = promptBox();
        clearedPrompt = ta ? ta.value : null;
        const host = document.getElementById('clear-all-btn');
        const btn = host && (host.tagName === 'BUTTON' ? host : host.querySelector('button'));
        if (btn) btn.click();
        flashPill(clearedPrompt ? 'Cleared ✓  ·  Alt+Z restores the prompt' : 'Cleared ✓');
    }
    function restoreClearedPrompt() {
        const ta = promptBox();
        if (!ta || clearedPrompt == null) { flashPill('Nothing to restore'); return; }
        setNative(ta, clearedPrompt);
        clearedPrompt = null;
        ta.focus();
        flashPill('Prompt restored ✓');
    }
    document.addEventListener('keydown', (e) => {
        if (previewOpen()) return;                    // the viewer owns the keyboard
        // ---- Ctrl+, : the universal "preferences" alias for Alt+S ----
        if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key === ',') {
            e.preventDefault();
            toggleSettingsPop();
            return;
        }
        // ---- "/" focuses the prompt, but only when you aren't already typing ----
        if (e.key === '/' && !e.ctrlKey && !e.metaKey && !e.altKey && !isTypingIn(e.target)) {
            e.preventDefault();
            focusPrompt(false);
            return;
        }
        if (!e.altKey || e.ctrlKey || e.metaKey) return;
        // Alt+` is matched by PHYSICAL key so layouts where that key is a dead
        // accent still work; e.key is the fallback.
        const backtick = e.code === 'Backquote' || e.key === '`' || e.key === '~';
        const k = (e.key || '').toLowerCase();
        if (backtick)      { e.preventDefault(); focusPrompt(true); }
        else if (k === 's') { e.preventDefault(); toggleSettingsPop(); }
        else if (k === 'l') { e.preventDefault(); openSecondsDropdown(); }
        else if (k === 'c') { e.preventDefault(); clearAllViaShortcut(); }
        else if (k === 'z') { e.preventDefault(); restoreClearedPrompt(); }
    }, true);

    // desktop: the page itself never scrolls — the grid is the only scroller.
    // (Must be JS: Gradio scopes custom CSS inside gradio-app, so a `body {}` rule
    // written in the CSS string never reaches the real <body>.)
    if (window.matchMedia('(min-width: 761px)').matches) {
        document.documentElement.style.overflowY = 'hidden';
        document.body.style.overflowY = 'hidden';
    }

    paint();
    enforceCardFit();
    setupFlowChrome();
    updateFoldCount();
    compactTopbarMobile();
    setupPromptGrow();
    // phones: a shorter placeholder so the prompt line never clips
    if (mobileUi) {
        const ta = document.querySelector('#prompt-box textarea');
        if (ta) ta.placeholder = 'Describe it…';
    }
    // keep the model pill + Ingredients-mode frame hiding in sync with the controls
    setInterval(() => { updateModelPill(); syncVmodeFrames(); }, 500);
    console.log('Bananza frontend v6.5');
}
"""

SETUP_GUIDE = """
## 🚀 One-time setup — about 5 minutes

The app runs on your **Google Cloud $300 free trial**. You only do this once.

---

### Step 1 · Copy your Project ID
Open [console.cloud.google.com](https://console.cloud.google.com). At the very top of the page,
click the **project name** (next to the "Google Cloud" logo). A window pops up with a list —
copy the value in the **ID** column (looks like `banana-app-448212`, letters and dashes, not just numbers).

### Step 2 · Turn on the image API
Click here: [enable the API](https://console.cloud.google.com/apis/library/aiplatform.googleapis.com)
and press the big blue **Enable** button. If it already says *"API Enabled"*, skip ahead.

### Step 3 · Install Google's sign-in tool
Download the **Google Cloud CLI** installer: [cloud.google.com/sdk/docs/install](https://cloud.google.com/sdk/docs/install).
Run it and just click **Next** through everything (all defaults are fine).

### Step 4 · Sign in — one time only
Press the **Windows key**, type `cmd`, press **Enter**. In the black window, paste this and press Enter:

```
gcloud auth application-default login
```

Your browser opens — **sign in with the same Google account** that has the free trial, and click Allow.
When it says success, you can close the black window.

### Step 5 · Connect this app
Open **⚙️ Settings** at the top of the app → paste your **Project ID** → **Save** → **Test connection**.

✅ Green message = you're done. Close this guide and start generating!

---

### 😵 If Test connection fails
- *"quota project"* error → back in the black window, run
  `gcloud auth application-default set-quota-project YOUR-PROJECT-ID` (use your real ID)
- *403 / permission* error → redo **Step 2**, wait 2 minutes, test again
- *"gcloud not recognized"* → finish **Step 3**, then open a **new** cmd window for **Step 4**

---

### 🎬 Making videos (v4.0)
Flip the **🖼 Image / 🎬 Video** toggle in the composer — the Model dropdown switches to the
video models. A **Video mode** picker appears with two ways to work:

- **🎞 Frames (start → end)** — drop a picture into the **Start frame** box and every clip
  starts on it. The **End frame is optional**: add one and Veo 3.1 models animate from your
  start picture to your end picture. **Omni Flash can't use an End frame** (the box greys
  out) — the Start frame still works.
- **🧺 Ingredients** — works exactly like images:
  no ingredients → your prompt becomes a video (Outputs = how many clips) ·
  ingredients, no tags → each tray image is animated into its own clip ·
  **@1 with a Veo model** → that image becomes the clip's first frame ·
  **Omni Flash** can combine several @tags into one video.

Where things go (new in v4.0):

- Every project now has two folders inside it: **`<Project> (images)`** and
  **`<Project> (videos)`** — generations land in the right one automatically, and files
  from older versions are moved over the first time you start the app.
- The **🗂 filter** in the View panel (top-right, ⚙ button) switches the canvas between **All / Images / Videos**.
- **Videos can't be ingredients** (Google's models only take images as input) — but the
  ↻ reuse button works on videos and brings back their full prompt & settings.

Good to know:

- Videos take **1–5 minutes each**. The progress card keeps counting while Google works —
  leave the app open and they'll drop into the canvas when ready.
- Ratio must be **▭ 16:9** or **▯ 9:16** (✨ auto counts as 16:9).
- **Videos are billed per second**, not per clip (estimates): Veo 3.1 $0.40 · 3.1 Fast $0.15 ·
  3.1 Lite $0.05 · Veo 3 $0.40 · 3 Fast $0.15 · Veo 2 $0.50 (no sound) · Omni Flash ≈ $0.10.
  An 8-second Veo 3.1 clip ≈ **$3.20** — Veo 3.1 Lite is great for trying prompts cheaply
  (≈ $0.40 per 8s clip). The estimated cost pops up the moment you press Generate.
- Start→End frames need a **Veo 3.1** model — older Veo models will tell you in plain
  English if they can't do it.
- **Omni Flash is brand-new (preview).** It makes ~10-second clips with sound and picks its
  own length. If it errors with *"isn't switched on for Vertex"*, Google hasn't enabled it
  for Vertex projects yet — use a Veo model until they do.
- Seeing *quota* or *rate limit* errors? Videos have much tighter limits than images —
  the app already caps video jobs at 2 at a time; just wait a minute and try again.

---

### ⌨️ Cheatsheet — everything the mouse & keyboard can do

**Making images**
- **Ctrl+Enter** in the prompt box = press Generate · runs stack, images appear live
- No ingredients → pure generation (**Outputs** = how many variations)
- With ingredients → the prompt is applied to **each** ingredient separately
- Type `@1` / `@2` in the prompt → combines exactly those ingredients into one image

**Reference images (ingredients)**
- **Ctrl+V** anywhere, **drag & drop** files onto the page, or the **＋ Add images** button
- Hover an ingredient: **@** drops its tag into the prompt · **✖** removes it (tags renumber)
- **Clear all** empties the tray *and* the prompt

**The canvas**
- **Click** an image → fullscreen viewer: **←/→** browse, **Del** deletes, **Ctrl+C** copies,
  **Esc** closes — plus buttons for reuse ↻, add-to-ingredients +, copy, download, delete
- **Ctrl+click** toggles selection, **Shift+click** selects a range, **drag** a box to lasso,
  **Ctrl+A** selects all, **Esc** clears
- **Del** deletes the selection (2 or more asks for confirmation first)
- **Ctrl+C** with one image selected copies it to the clipboard
- Hover any image for quick actions: ↻ reuse prompt · + make ingredient · 📋 copy · delete
- **↻ reuse** brings back everything that made it: prompt, model, settings, **and** the
  Start/End frames and reference images it used
- Deleted things always land in `output/.trash` — recoverable, never destroyed
"""


def err(msg):
    return gr.Error(msg, duration=TOAST_SECONDS)


# ---------------- config ----------------

def load_config():
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def update_config(**kwargs):
    cfg = load_config()
    cfg.update(kwargs)
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=1))
    except Exception:
        gr.Warning(f"Couldn't save settings to {CONFIG_FILE} — is the folder read-only?",
                   duration=TOAST_SECONDS)


def _migrate_config():
    """One-time config upgrades. v2: 'images at the same time' default becomes 3
    (older configs saved 2 — bump once, then respect whatever the user picks)."""
    cfg = load_config()
    if int(cfg.get("cfg_v", 1)) < 2:
        cfg["workers"] = 3
        cfg["cfg_v"] = 2
        try:
            CONFIG_FILE.write_text(json.dumps(cfg, indent=1))
        except Exception:
            pass


def save_settings(project, location):
    project = (project or "").strip()
    update_config(project=project, location=(location or "").strip() or "global")
    if not project:
        return "⚠️ Saved, but the Project ID is empty — see the Setup guide."
    return "✅ Saved — loads automatically next time. Now hit Test connection."


# ---------------- remember-my-settings (v4.9 — replaced presets) ----------------
# Every composer control writes straight to config.json the moment it changes, so
# the app reopens exactly as it was left. (Generating saves too, incl. media/vmode,
# which are radios — Gradio radio .change is unreliable, so they save on generate.)

def save_ui_state(mimg, mvid, aspect, size, count, vres, vdur, vaudio):
    try:
        count = max(1, min(int(float(count)), 100))
    except (TypeError, ValueError):
        count = 1
    update_config(model_image=mimg, model_video=mvid, aspect=aspect, size=size,
                  count=count, vres=vres,
                  vdur=snap_dur(mvid, vdur),      # v6.0: per-model allowed lengths
                  vaudio=bool(vaudio))


# ---------------- client ----------------

def make_client(project, location):
    project = (project or "").strip()
    location = (location or "").strip() or "global"
    if not project:
        raise err("No Project ID set — open ⚙️ Settings, or click Setup guide if it's your first time.")
    try:
        return genai.Client(vertexai=True, project=project, location=location)
    except Exception as e:
        if "credential" in str(e).lower():
            raise err("Google sign-in not found — do Step 4 of the Setup guide "
                      "(`gcloud auth application-default login`).")
        raise


def test_connection(project, location):
    try:
        client = make_client(project, location)
        next(iter(client.models.list()))
        return "✅ **Connected!** Your free-trial project is ready — go generate."
    except gr.Error as e:
        return f"❌ {getattr(e, 'message', e)}"
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
        return f"❌ {type(e).__name__}: {msg[:400]}{hint}"


# ---------------- spend tracker ----------------

def load_usage():
    if USAGE_FILE.exists():
        try:
            return json.loads(USAGE_FILE.read_text())
        except Exception:
            pass
    return {"images": 0, "est_cost": 0.0}


def save_usage(usage):
    try:
        USAGE_FILE.write_text(json.dumps(usage))
    except Exception:
        gr.Warning(f"Couldn't save the spend tracker to {USAGE_FILE} — all-time totals "
                   "won't survive a relaunch. Is the folder read-only or cloud-synced?",
                   duration=TOAST_SECONDS)


def add_usage(images=1, cost=0.0):
    with _usage_lock:
        u = load_usage()
        u["images"] += images
        u["est_cost"] += cost
        save_usage(u)
        SESSION["images"] += images
        SESSION["cost"] += cost


def top_stats():
    u = load_usage()
    return (
        f"<div id='topstats'>🖼 {u['images']} · ~${u['est_cost']:.2f} / $300"
        " &nbsp;·&nbsp; <a href='https://console.cloud.google.com/billing' target='_blank'>credits ↗</a></div>"
    )


def stat_cards():
    u = load_usage()

    def card(value, label):
        return (
            f"<div style='flex:1;min-width:140px;text-align:center;padding:12px 10px;border-radius:14px;"
            f"background:rgba({ACCENT},.08);border:1px solid rgba({ACCENT},.28)'>"
            f"<div style='font-size:1.25rem;font-weight:700'>{value}</div>"
            f"<div style='font-size:.72rem;opacity:.7;margin-top:2px'>{label}</div></div>"
        )

    return (
        "<div style='display:flex;gap:10px;flex-wrap:wrap'>"
        + card(SESSION["images"], "Items this session")
        + card(f"${SESSION['cost']:.2f}", "Est. cost this session")
        + card(u["images"], "Items all time")
        + card(f"${u['est_cost']:.2f}", "Est. cost all time")
        + "</div>"
        "<div style='font-size:.75rem;opacity:.6;margin-top:6px'>"
        "Estimates use list prices — images: 1K $0.067 · 2K $0.101 · 4K $0.151 each · "
        "videos are billed per second: Veo 3.1 $0.40 · 3.1 Fast $0.15 · 3.1 Lite $0.05 · "
        "Veo 3 $0.40 · 3 Fast $0.15 · Veo 2 $0.50 · Omni Flash ≈$0.10. Vertex prices "
        "may differ slightly. Your real balance: "
        "<a href='https://console.cloud.google.com/billing' target='_blank'>Cloud billing → Credits</a></div>"
    )


def reset_tracker():
    save_usage({"images": 0, "est_cost": 0.0})
    return stat_cards(), top_stats()


# ---------------- projects (output folders) ----------------

def sanitize_name(name):
    return re.sub(r"[^\w\- ]", "", (name or "")).strip()[:60]


def migrate_unsorted():
    OUT_DIR.mkdir(exist_ok=True)
    unsorted = OUT_DIR / UNSORTED
    unsorted.mkdir(exist_ok=True)
    for f in OUT_DIR.iterdir():
        if f.is_file() and f.suffix.lower() in MEDIA_EXTS:
            try:
                shutil.move(str(f), str(unsorted / f.name))
            except Exception:
                pass


def migrate_media_split():
    """v4.0: every project now keeps media in two subfolders —
    output/<Project>/<Project> (images)/ and .../<Project> (videos)/.
    Move any loose project-root files into the right one and split the root
    .meta.json between them. Safe to run on every start (does nothing twice)."""
    OUT_DIR.mkdir(exist_ok=True)
    for proj in OUT_DIR.iterdir():
        if not proj.is_dir() or proj.name == ".trash":
            continue
        try:
            img_d = proj / f"{proj.name} (images)"
            vid_d = proj / f"{proj.name} (videos)"
            img_d.mkdir(exist_ok=True)
            vid_d.mkdir(exist_ok=True)
            for f in list(proj.iterdir()):
                if not (f.is_file() and f.suffix.lower() in MEDIA_EXTS):
                    continue
                dest = (vid_d if f.suffix.lower() in VIDEO_EXTS else img_d) / f.name
                if dest.exists():   # same name already there — keep both files
                    dest = dest.with_name(f"{f.stem}_{int(f.stat().st_mtime)}{f.suffix}")
                shutil.move(str(f), str(dest))
            root_meta = proj / ".meta.json"
            if root_meta.exists():
                m = _load_meta(proj)
                for d, exts in ((img_d, IMAGE_EXTS), (vid_d, VIDEO_EXTS)):
                    part = {k: v for k, v in m.items()
                            if Path(k).suffix.lower() in exts}
                    if part:
                        merged = _load_meta(d)
                        merged.update(part)
                        try:
                            (d / ".meta.json").write_text(
                                json.dumps(merged, ensure_ascii=False), encoding="utf-8")
                        except Exception:
                            pass
                try:
                    root_meta.unlink()
                except Exception:
                    pass
        except Exception:
            pass   # one broken project must never stop the app from starting


def list_projects():
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / UNSORTED).mkdir(exist_ok=True)
    subs = sorted([d.name for d in OUT_DIR.iterdir()
                   if d.is_dir() and d.name not in (UNSORTED, ".trash")], key=str.lower)
    return [UNSORTED] + subs


def project_dir(name):
    OUT_DIR.mkdir(exist_ok=True)
    d = OUT_DIR / (sanitize_name(name) or UNSORTED)
    d.mkdir(exist_ok=True)
    return d


# ---- v4.0: per-project media subfolders ----

def images_dir(name):
    """output/<Project>/<Project> (images)/ — where generated images land."""
    d = project_dir(name)
    sub = d / f"{d.name} (images)"
    sub.mkdir(exist_ok=True)
    return sub


def videos_dir(name):
    """output/<Project>/<Project> (videos)/ — where generated videos land."""
    d = project_dir(name)
    sub = d / f"{d.name} (videos)"
    sub.mkdir(exist_ok=True)
    return sub


def _media_dirs(name):
    """Everywhere a project's files may live (project root last = legacy files
    made before v4.0 or dropped in by hand — they still show up)."""
    return [images_dir(name), videos_dir(name), project_dir(name)]


def sources_dir(name):
    """output/<Project>/.sources/ — v6.4. Copies of the Start/End frames and
    reference images a run was given, so ↻ Reuse can hand them back later. It's a
    directory and project_media() only ever lists files, so it can't show up in the
    canvas — same trick as .trash."""
    d = project_dir(name) / ".sources"
    d.mkdir(exist_ok=True)
    return d


def _is_temp_upload(p):
    """True for a file sitting in Gradio's upload area — those are wiped when the
    app restarts, so anything we want to remember has to be copied out first."""
    roots = [Path(tempfile.gettempdir()).resolve()]
    if os.environ.get("GRADIO_TEMP_DIR"):
        roots.append(Path(os.environ["GRADIO_TEMP_DIR"]).resolve())
    try:
        return any(Path(p).resolve().is_relative_to(r) for r in roots)
    except Exception:
        return False


def keep_source(path, project_name):
    """Path to remember for one source image. Files already inside the project are
    referenced where they are; temp uploads get copied into .sources under a
    content-hash name (so the same image added twice is stored once). Returns None
    if anything goes wrong — a missing reference must never break a generation."""
    try:
        p = Path(path)
        if not p.is_file():
            return None
        if not _is_temp_upload(p):
            return str(p)
        data = p.read_bytes()
        dst = sources_dir(project_name) / f"{hashlib.sha1(data).hexdigest()[:16]}{p.suffix.lower()}"
        if not dst.exists():
            dst.write_bytes(data)
        return str(dst)
    except Exception:
        return None


def keep_sources(paths, project_name):
    return [s for s in (keep_source(p, project_name) for p in (paths or [])) if s]


def find_media(name, basename):
    """Locate one file by basename across the project's folders.
    Basename only — path escapes are impossible by construction."""
    if not basename:
        return None
    safe = Path(basename).name
    for d in _media_dirs(name):
        p = d / safe
        if p.exists() and p.is_file():
            return p
    return None


def project_media(name, media_filter="All"):
    """What the canvas shows — All / Images / Videos, newest first."""
    want = (IMAGE_EXTS if media_filter == "Images"
            else VIDEO_EXTS if media_filter == "Videos" else MEDIA_EXTS)
    files, seen = [], set()
    for d in _media_dirs(name):
        for f in d.iterdir():
            if f.is_file() and f.suffix.lower() in want and f.name not in seen:
                seen.add(f.name)
                files.append(f)
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return [str(f) for f in files]


def project_images(name):
    """Back-compat helper: every item in the project (used for counts)."""
    return project_media(name, "All")


# ---------------- per-image prompt metadata (powers "reuse prompt") ----------------

_meta_lock = threading.Lock()


def _load_meta(d):
    p = d / ".meta.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def record_meta(dst, info):
    """Store the prompt + settings that produced dst (called per finished image)."""
    with _meta_lock:
        d = dst.parent
        m = _load_meta(d)
        m[dst.name] = info
        try:
            (d / ".meta.json").write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


def meta_for(basename, project_name):
    if not basename:
        return None
    p = find_media(project_name, basename)
    dirs = ([p.parent] if p else []) + [project_dir(project_name)]
    for d in dirs:
        m = _load_meta(d).get(Path(basename).name)
        if m:
            return m
    return None


def _store_basename(store_json):
    try:
        return Path(json.loads(store_json or "{}").get("n", "")).name or None
    except Exception:
        return None


def _alive(paths):
    """Keep only the source files that are still on disk, and say how many are
    missing. Empty slots don't count as missing — a video with no End frame simply
    never had one, and warning about that would be a lie on every single reuse."""
    asked = [p for p in (paths or []) if p]
    kept = [p for p in asked if Path(p).is_file()]
    return kept, len(asked) - len(kept)


def reuse_prompt(store_json, project_name):
    """↻ hover button: refill prompt + model/ratio/resolution from the item's
    metadata. Works for videos too — it flips the composer to Video for you.
    v6.4 also puts back the Start/End frames and the reference images the run used.
    Returns 14 values; the wiring chains composer_visibility after this so the
    right controls show for whatever was restored."""
    basename = _store_basename(store_json)
    m = meta_for(basename, project_name) if basename else None
    if not m:
        gr.Warning("No saved prompt for this item (it was made before prompt-saving existed).",
                   duration=TOAST_SECONDS)
        return (gr.update(),) * 14
    aspect_v = m.get("aspect") if m.get("aspect") in [v for _, v in ASPECT_CHOICES] else "auto"
    is_vid = m.get("kind") == "video" or m.get("model") in VIDEO_MODELS
    media_v = "video" if is_vid else "image"
    model_img_v = m.get("model") if (not is_vid and m.get("model") in MODELS) else gr.update()
    model_vid_v = m.get("model") if (is_vid and m.get("model") in VIDEO_MODELS) else gr.update()
    size_v = m.get("size") if m.get("size") in ("1K", "2K", "4K") else gr.update()
    vres_v = m.get("size") if m.get("size") in VIDEO_RESOLUTIONS else gr.update()
    vdur_v = (snap_dur(m.get("model"), m["dur"]) if m.get("dur") else gr.update())
    vaudio_v = bool(m["audio"]) if isinstance(m.get("audio"), bool) else gr.update()
    vmode_v = m.get("vmode") if m.get("vmode") in ("frames", "ingredients") else gr.update()

    # v6.4: the images that went in. Anything since deleted is skipped rather than
    # restored as a broken path, and we say so once instead of failing.
    frames, gone = _alive([m.get("frame_start"), m.get("frame_end")])
    fstart_v = m.get("frame_start") if m.get("frame_start") in frames else None
    fend_v = m.get("frame_end") if m.get("frame_end") in frames else None
    refs, ref_gone = _alive(m.get("refs"))
    gone += ref_gone
    if gone:
        gr.Warning(f"Prompt & settings restored, but {gone} of the images it used "
                   "aren't on disk any more.", duration=TOAST_SECONDS)
    else:
        gr.Info("Prompt, settings, frames & references restored — tweak anything, "
                "then Generate.", duration=4)
    return (m.get("prompt", ""), media_v, model_img_v, model_vid_v, aspect_v, size_v,
            ratio_preview(aspect_v), vres_v, vdur_v, vaudio_v, vmode_v,
            fstart_v, fend_v, refs)


def add_ingredient(store_json, project_name, queue, media, vmode, model_vid,
                   fstart, fend):
    """+ hover button: drop a generated image into the composer. In Frames mode it
    becomes the Start/End frame instead of a tray ingredient (v6.0 routing).
    Videos can never be ingredients — ↻ reuse is their path back into the composer."""
    basename = _store_basename(store_json)
    if basename and basename.lower().endswith(VIDEO_EXTS):
        gr.Warning("Videos can't be ingredients — but ↻ still reuses their prompt & settings.",
                   duration=TOAST_SECONDS)
        return queue, gr.update(), gr.update(), gr.update()
    p = find_media(project_name, basename)
    if not p:
        gr.Warning("Couldn't find that image on disk anymore.", duration=TOAST_SECONDS)
        return queue, gr.update(), gr.update(), gr.update()
    before = len(queue)
    queue, fstart, fend, notes = route_added_images(
        [str(p)], queue, media, vmode, model_vid, fstart, fend)
    if not notes and len(queue) > before:
        notes = [f"Added as ingredient @{len(queue)} — reference it in your prompt."]
    return _routed_updates(queue, fstart, fend, notes)


def describe_selected(evt_path, project_name):
    """Caption under the canvas showing the selected image's prompt."""
    if not evt_path:
        return ""
    basename = Path(evt_path).name
    icon = "🎬" if basename.lower().endswith(VIDEO_EXTS) else "🖼"
    m = meta_for(basename, project_name)
    if not m:
        return f"<span id='sel-caption'>{icon} {html.escape(basename)}</span>"
    p = (m.get("prompt") or "").replace("\n", " ")
    if len(p) > 160:
        p = p[:160] + "…"
    p = html.escape(p)
    extra = f" · {m['dur']}s" if m.get("kind") == "video" and m.get("dur") else ""
    return (f"<span id='sel-caption'>{icon} <b>{html.escape(basename)}</b> · “{p}” · "
            f"{m.get('size', '?')} · {m.get('aspect', 'auto')}{extra}</span>")


def composer_visibility(media, vid_label, vmode, vdur=None):
    """One source of truth for which composer controls show.
    v6.0: the Seconds dropdown also re-lists itself, because each video model
    accepts a different set of clip lengths (model["durs"])."""
    vid = media == "video"
    m = VIDEO_MODELS.get(vid_label, {})
    omni = m.get("kind") == "omni"
    veo2 = m.get("id", "").startswith("veo-2")
    frames = vid and vmode == "frames"
    durs = model_durs(vid_label)
    dur_upd = gr.update(visible=vid, choices=durs,
                        value=snap_dur(vid_label, vdur if vdur is not None else 8))
    if omni:
        # Omni Flash has no end-frame support: grey the slot out (and clear it)
        # but leave the Start frame fully usable.
        end_upd = gr.update(visible=frames, interactive=False, value=None,
                            label="End frame — Omni Flash can't use one")
    else:
        end_upd = gr.update(visible=frames, interactive=True,
                            label="End frame (optional)")
    return (gr.update(visible=not vid),                        # image model dropdown
            gr.update(visible=vid),                            # video model dropdown
            gr.update(visible=not vid),                        # 1K/2K/4K (images only)
            gr.update(visible=vid and not omni and not veo2),  # 720p/1080p
            dur_upd,                                           # seconds (per-model choices)
            gr.update(visible=vid and bool(m.get("audio"))),   # sound — hidden for Omni:
                                                               # its API takes no audio flag
            gr.update(visible=vid),                            # Frames/Ingredients row
            gr.update(visible=frames),                         # start frame slot (Frames mode only)
            end_upd)                                           # end frame slot (Frames mode only)


def change_filter(flt, name):
    """🗂 All / 🖼 Images / 🎬 Videos canvas filter."""
    update_config(media_filter=flt)
    return project_media(name, flt), None, ""


def change_grid(n, _name):
    update_config(grid=int(n))
    return gr.update(columns=int(n))


def bump():
    global DATA_VERSION
    with RUNS_LOCK:
        DATA_VERSION += 1


def _fmt_secs(s):
    s = int(s)
    return f"{s // 60}m {s % 60:02d}s" if s >= 60 else f"{s}s"


def build_status(name):
    """Live run-cards only (v4.7): the old '📁 folder · N item(s)' header moved to
    the top bar (JS fills #fold-count), so when nothing is running this returns ""
    and the bottom of the screen shows nothing but the floating pill."""
    parts = []
    with RUNS_LOCK:
        runs = [dict(r) for r in RUNS.values()]
    for r in runs:
        if r["finished"]:
            continue
        total = max(r["total"], 1)
        pct = int(r["done"] * 100 / total)
        elapsed = time.time() - r["started"]
        eta = ""
        if 0 < r["done"] < total:
            eta = f" · ~{_fmt_secs(elapsed / r['done'] * (total - r['done']))} left"
        fails = (f" · <span class='run-err'>{len(r['errors'])} failed</span>"
                 if r["errors"] else "")
        indet = " indet" if r["done"] == 0 else ""
        parts.append(
            "<div class='run-card'>"
            f"<div class='run-top'><span>⏳ Run {r['id']} · {html.escape(r['mode'])} "
            f"→ 📁 {html.escape(r['project'])}</span>"
            f"<span>{r['done']}/{r['total']} · {_fmt_secs(elapsed)}{eta}{fails}</span></div>"
            f"<div class='run-bar{indet}'><i style='width:{max(pct, 4)}%'></i></div>"
            "</div>"
        )
    finished = [r for r in runs if r["finished"]]
    for r in finished[-3:]:
        if r["errors"]:
            parts.append(f"<div class='run-done warn'>⚠️ Run {r['id']} · "
                         f"{html.escape(r['mode'])} · {r['ok']}/{r['total']} ok · "
                         f"{len(r['errors'])} failed — {html.escape(r['errors'][0][:140])}</div>")
        else:
            parts.append(f"<div class='run-done'>✅ Run {r['id']} · {html.escape(r['mode'])} "
                         f"→ 📁 {html.escape(r['project'])} · {r['ok']}/{r['total']} ok</div>")
        # v6.4: e.g. "Google returned ~10s clips, not the 4s you picked"
        for n in sorted(r.get("notes") or ()):
            parts.append(f"<div class='run-done warn'>⚠️ {html.escape(n)}</div>")
    return "".join(parts)


def switch_project(name, media_filter):
    update_config(last_project=name)
    return project_media(name, media_filter), build_status(name), None


def create_project(new_name, current, media_filter):
    name = sanitize_name(new_name)
    if not name:
        gr.Warning("Type a name for the new project first.", duration=TOAST_SECONDS)
        return gr.update(), gr.update(), gr.update(), gr.update()
    project_dir(name)
    images_dir(name)     # v4.0: the (images)/(videos) folders exist from day one
    videos_dir(name)
    update_config(last_project=name)
    imgs, label, _ = switch_project(name, media_filter)
    return gr.update(choices=list_projects(), value=name), "", imgs, label


def ask_delete_project(name):
    if name == UNSORTED:
        gr.Warning("The Unsorted project can't be deleted.", duration=TOAST_SECONDS)
        return gr.update(visible=False), gr.update()
    n = len(project_images(name))
    return (gr.update(visible=True),
            f"**Delete project “{name}” and its {n} item(s)?** "
            "Everything moves to `output/.trash` (recoverable), but it will disappear from the app.")


def confirm_delete_project(name, media_filter):
    if name != UNSORTED:
        src = OUT_DIR / sanitize_name(name)
        if src.exists():
            TRASH_DIR.mkdir(exist_ok=True)
            shutil.move(str(src), str(TRASH_DIR / f"{src.name}_{time.strftime('%Y%m%d_%H%M%S')}"))
            gr.Info(f"Project '{name}' moved to output/.trash.", duration=TOAST_SECONDS)
    update_config(last_project=UNSORTED)
    return (gr.update(choices=list_projects(), value=UNSORTED),
            project_media(UNSORTED, media_filter), build_status(UNSORTED),
            gr.update(visible=False))


def on_result_select(evt: gr.SelectData, name, media_filter):
    imgs = project_media(name, media_filter)
    if evt.index is not None and 0 <= evt.index < len(imgs):
        path = imgs[evt.index]
        return path, describe_selected(path, name)
    return None, ""


# ---------------- deleting images (single + multi-select) ----------------

def _trash_file(p):
    TRASH_DIR.mkdir(exist_ok=True)
    shutil.move(str(p), str(TRASH_DIR / f"{time.strftime('%H%M%S')}_{p.name}"))


# ---------------- trash manager (v4.9: recover / purge without Explorer) ----------------

def list_trash():
    """Trashed media, newest first, as [(path, label)] for a gr.Gallery."""
    if not TRASH_DIR.exists():
        return []
    files = [p for p in TRASH_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [(str(p), _trash_orig_name(p.name)) for p in files]


def _trash_orig_name(fname):
    """Undo either trash-naming scheme: 'HHMMSS_name.png' or 'name.png_YYYYMMDD_HHMMSS'."""
    m = re.match(r"^\d{6}_(.+)$", fname)
    if m:
        return m.group(1)
    m = re.match(r"^(.+?)_\d{8}_\d{6}$", fname)
    if m:
        return m.group(1)
    return fname


def _trash_paths():
    return [Path(p) for p, _label in list_trash()]


def trash_heading():
    """Heading with a live count so the trash is obvious at a glance (v6.0)."""
    n = len(list_trash())
    return f"#### 🗑 Trash — {n} item{'s' if n != 1 else ''}" if n else "#### 🗑 Trash — empty"


def restore_trash(idx, project_name):
    """Move the selected trash item back into the OPEN project (by media type).
    v6.4: the caller re-renders the main grid — the poll timer is parked when
    nothing is generating, so this can no longer rely on a tick to pick it up."""
    items = _trash_paths()
    if idx is None or not (0 <= int(idx) < len(items)):
        gr.Warning("Tap an item in the trash first.", duration=4)
        return gr.update(), None
    src = items[int(idx)]
    orig = _trash_orig_name(src.name)
    dest_dir = videos_dir(project_name) if Path(orig).suffix.lower() in VIDEO_EXTS \
        else images_dir(project_name)
    dest = dest_dir / orig
    n = 1
    while dest.exists():                       # collision-safe: name (2).png, …
        dest = dest_dir / f"{Path(orig).stem} ({n + 1}){Path(orig).suffix}"
        n += 1
    shutil.move(str(src), str(dest))
    bump()
    gr.Info(f"Restored {dest.name} to “{project_name}”.", duration=5)
    return gr.update(value=list_trash()), None


def purge_trash(idx):
    """Permanently delete ONE selected trash item."""
    items = _trash_paths()
    if idx is None or not (0 <= int(idx) < len(items)):
        gr.Warning("Tap an item in the trash first.", duration=4)
        return gr.update(), None
    items[int(idx)].unlink(missing_ok=True)
    gr.Info("Deleted forever.", duration=4)
    return gr.update(value=list_trash()), None


def empty_trash():
    """Permanently delete EVERYTHING in the trash."""
    n = 0
    for p in _trash_paths():
        p.unlink(missing_ok=True)
        n += 1
    gr.Info(f"Trash emptied — {n} item(s) gone for good." if n
            else "Trash was already empty.", duration=4)
    return gr.update(value=list_trash()), None


def _selected_paths(sel_json, name):
    """Map the browser's selection (JSON list of file names) back to real files.
    find_media takes basenames only — path escapes are impossible."""
    try:
        names = json.loads(sel_json or "[]")
    except Exception:
        names = []
    out = []
    for n in names:
        if not isinstance(n, str):
            continue
        p = find_media(name, n)
        if p and p.suffix.lower() in MEDIA_EXTS:
            out.append(p)
    return out


def delete_images(sel_json, sel_path, name, media_filter):
    """Delete button: 1 item deletes right away, 2+ asks for confirmation first."""
    paths = _selected_paths(sel_json, name)
    if len(paths) >= 2:
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(visible=True),
                f"**Delete the {len(paths)} selected items from “{name}”?** "
                "They move to `output/.trash` — recoverable, never destroyed.")
    target = paths[0] if paths else (Path(sel_path) if sel_path else None)
    if not target or not target.exists():
        gr.Warning("Select images first — Ctrl+click, or drag a box around them — then hit delete.",
                   duration=TOAST_SECONDS)
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(visible=False), gr.update())
    _trash_file(target)
    gr.Info(f"Deleted {target.name} (recoverable in output/.trash).", duration=5)
    return (project_media(name, media_filter), build_status(name), None, "[]",
            gr.update(visible=False), gr.update())


def confirm_delete_images(sel_json, name, media_filter):
    paths = _selected_paths(sel_json, name)
    for p in paths:
        _trash_file(p)
    if paths:
        gr.Info(f"Deleted {len(paths)} items — recoverable in output/.trash.", duration=5)
    return (project_media(name, media_filter), build_status(name), None, "[]",
            gr.update(visible=False))


def open_output_folder(name):
    d = project_dir(name)
    try:
        system = platform.system()
        if system == "Windows":
            os.startfile(d)  # noqa
        elif system == "Darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
        gr.Info(f"Opened the '{name}' folder.", duration=4)
    except Exception:
        gr.Info(f"Folder: {d.resolve()}", duration=TOAST_SECONDS)


# ---------------- aspect ratio preview ----------------

def ratio_preview(aspect):
    if not aspect or aspect == "auto":
        inner = ("<div style='width:30px;height:30px;border:2px dashed rgba(167,139,250,.7);"
                 "border-radius:6px;display:flex;align-items:center;justify-content:center;"
                 "font-size:.6rem;opacity:.8'>auto</div>")
    else:
        w, h = (int(x) for x in aspect.split(":"))
        scale = 34 / max(w, h)
        bw, bh = max(round(w * scale), 12), max(round(h * scale), 12)
        inner = (f"<div style='width:{bw}px;height:{bh}px;border:2px solid #a78bfa;"
                 f"border-radius:4px;background:rgba({ACCENT},.15)'></div>")
    return f"<div id='ratio-box'>{inner}</div>"


# ---------------- generation (background workers) ----------------

def gen_image(client, model, prompt, image_paths, image_config):
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


def generate_one(client, model, prompt, image_paths, out_path, image_config):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            out_path.write_bytes(gen_image(client, model, prompt, image_paths, image_config))
            return str(out_path), None
        except Exception as e:
            msg = str(e).strip() or repr(e)
            retryable = any(c in msg for c in ("429", "500", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE"))
            if retryable and attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt + random.uniform(0, 2), 60))
            else:
                import traceback
                traceback.print_exc()  # full details in the terminal window
                return None, f"{type(e).__name__}: {msg}"[:600]
    return None, "Gave up after retries (rate limited?)"



# ---------------- video generation (v3.9) ----------------

def _mp4_duration_seconds(path):
    """Clip length straight from the MP4 header (moov→mvhd atom) — no ffmpeg.
    Used so the spend tracker bills the seconds Google ACTUALLY returned."""
    try:
        data = Path(path).read_bytes()
        i = data.find(b"mvhd")
        if i == -1:
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
    if vcfg.get("last"):
        # Frames mode with an End frame: Veo interpolates start → end.
        kwargs["last_frame"] = types.Image.from_file(location=str(vcfg["last"]))

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
            # The END FRAME is the one thing we never drop silently — losing it
            # would change what the user asked for, so that error stays loud.
            msg = str(e)
            if "last_frame" in kwargs and ("last_frame" in msg or "lastFrame" in msg):
                raise RuntimeError(
                    "This model can't do start→end frames — Veo 3.1 models can. "
                    "Switch to one of those, or clear the End frame and try again.")
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
    aspect ratio and (now) the length are configurable. If this Omni build doesn't
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
        # v6.4 — THE "Seconds dropdown does nothing" bug. This used to send
        # `duration_seconds`, which is a field of GenerateVideosConfig (the Veo
        # path) but does NOT exist on the Interactions API's video response
        # format. That type is VideoResponseFormat in
        # google/genai/_gaos/types/interactions/videoresponseformat.py, and its
        # field is `duration`, a protobuf Duration STRING ("8s"). The SDK types
        # response_format as a union that includes Dict[str, Any], so the wrong
        # key raised no validation error at all — it was simply handed to the
        # server and ignored, and every Omni clip came back at its native length.
        fmt["duration"] = f"{dur}s"
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


def generate_one_video(client, model_label, prompt, image_paths, out_path, vcfg):
    """Same contract as generate_one: returns (path, error). Runs inside the
    worker pool, so several clips can cook in parallel."""
    m = VIDEO_MODELS[model_label]
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            if m["kind"] == "omni":
                data = gen_video_omni(client, m["id"], prompt, image_paths, vcfg)
            else:
                data = gen_video_veo(client, m["id"], prompt,
                                     image_paths[0] if image_paths else None, vcfg)
            out_path.write_bytes(data)
            return str(out_path), None
        except Exception as e:
            msg = str(e).strip() or repr(e)
            if m["kind"] == "omni" and ("NOT_FOUND" in msg or "404" in msg
                                        or "not found" in msg.lower()):
                return None, ("Omni Flash isn't switched on for Vertex on your project "
                              "yet (it's brand-new and Google is still rolling it out). "
                              "Pick a Veo model instead for now.")
            retryable = any(c in msg for c in ("429", "500", "503",
                                               "RESOURCE_EXHAUSTED", "UNAVAILABLE"))
            if retryable and attempt < MAX_RETRIES:
                time.sleep(min(2 ** attempt + random.uniform(0, 2), 60))
            else:
                import traceback
                traceback.print_exc()  # full details in the terminal window
                return None, f"{type(e).__name__}: {msg}"[:600]
    return None, "Gave up after retries (rate limited?)"


def plan_video_jobs(prompt, queue, count, stamp, out_dir, model_label,
                    vmode="ingredients", frame_start=None, frame_end=None):
    """Video job planner — two user-picked modes (v4.0):
    Frames      → every clip starts on the Start frame (End frame optional;
                  Veo 3.1 interpolates start → end; Omni Flash has no end frame).
    Ingredients → the classic three shapes: no tray = text-to-video · tray =
                  animate each ingredient · @tags = that image is the first
                  frame (Omni Flash may combine several)."""
    multi_ok = VIDEO_MODELS[model_label]["kind"] == "omni"
    tags = [int(t) for t in TAG_RE.findall(prompt)]

    if vmode == "frames":
        if tags:
            raise err("@ tags belong to Ingredients mode — switch Video mode to "
                      "🧺 Ingredients, or remove the @ tags from your prompt.")
        if not frame_start:
            raise err("Frames mode needs a Start frame — add one in the Start frame "
                      "box below, or switch Video mode to 🧺 Ingredients.")
        if frame_end and multi_ok:
            raise err("Omni Flash can't use an End frame — clear it, or pick a "
                      "Veo 3.1 model for start→end videos.")
        jobs = [(prompt, [frame_start], out_dir / f"frames_{stamp}_{i + 1}.mp4")
                for i in range(count)]
        what = "Start→End frames" if frame_end else "Start frame"
        return f"{what} → video, {count} output(s)", jobs

    if tags:
        if not queue:
            raise err("Your prompt uses @ tags but the ingredient tray is empty.")
        bad = [t for t in tags if t < 1 or t > len(queue)]
        if bad:
            raise err(f"@{bad[0]} doesn't exist — the tray has {len(queue)} image(s) (@1–@{len(queue)}).")
        seen, ordered = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        if len(ordered) > 1 and not multi_ok:
            raise err("Veo models can animate ONE image per video. Keep a single @tag "
                      "(that image becomes the first frame) — or switch the model to "
                      "Omni Flash, which can combine several.")
        clean_prompt = TAG_RE.sub(lambda m: f"image {m.group(1)}", prompt)
        if len(ordered) > 1:
            clean_prompt = (
                f"You are given {len(ordered)} reference image(s), in order: "
                + ", ".join(f"image {t}" for t in ordered) + ". " + clean_prompt
            )
        images = [queue[t - 1] for t in ordered]
        jobs = [(clean_prompt, images, out_dir / f"video_combo_{stamp}_{i + 1}.mp4")
                for i in range(count)]
        word = "Animating" if len(ordered) == 1 else "Combining"
        return f"{word} {len(ordered)} tagged image(s) → video, {count} output(s)", jobs

    if queue:
        # v6.5: respect Outputs on "animate the tray" too — one clip per image per
        # pass, repeated `count` times. Previously Outputs was silently ignored
        # whenever the tray had something in it.
        jobs = [(prompt, [q], out_dir / f"{Path(q).stem}_video_{stamp}_{i + 1}.mp4")
                for q in queue for i in range(count)]
        extra = f", {count} output(s) each" if count > 1 else ""
        return f"Animating all {len(queue)} tray image(s){extra}", jobs

    jobs = [(prompt, [], out_dir / f"video_{stamp}_{i + 1}.mp4") for i in range(count)]
    return f"Text-to-video, {count} output(s)", jobs


def plan_jobs(prompt, queue, count, stamp, out_dir):
    tags = [int(t) for t in TAG_RE.findall(prompt)]

    if tags:
        if not queue:
            raise err("Your prompt uses @ tags but the ingredient tray is empty.")
        bad = [t for t in tags if t < 1 or t > len(queue)]
        if bad:
            raise err(f"@{bad[0]} doesn't exist — the tray has {len(queue)} image(s) (@1–@{len(queue)}).")
        seen, ordered = set(), []
        for t in tags:
            if t not in seen:
                seen.add(t)
                ordered.append(t)
        clean_prompt = TAG_RE.sub(lambda m: f"image {m.group(1)}", prompt)
        clean_prompt = (
            f"You are given {len(ordered)} reference image(s), in order: "
            + ", ".join(f"image {t}" for t in ordered) + ". " + clean_prompt
        )
        images = [queue[t - 1] for t in ordered]
        jobs = [(clean_prompt, images, out_dir / f"combo_{stamp}_{i + 1}.png") for i in range(count)]
        return f"Combining {len(ordered)} tagged image(s), {count} output(s)", jobs

    if queue:
        # v6.5: respect Outputs on "edit the tray" too — one image per ref per
        # pass, repeated `count` times. Previously Outputs was silently ignored
        # whenever the tray had something in it.
        jobs = [(prompt, [p], out_dir / f"{Path(p).stem}_nb2_{stamp}_{i + 1}.png")
                for p in queue for i in range(count)]
        extra = f", {count} output(s) each" if count > 1 else ""
        return f"Editing all {len(queue)} tray image(s){extra}", jobs

    jobs = [(prompt, [], out_dir / f"banana_{stamp}_{i + 1}.png") for i in range(count)]
    return f"Text-to-image, {count} output(s)", jobs


# ---------------- v6.0: what will this run cost, BEFORE you hit Generate ----------------
# Uses the real planners, so the number matches what the run itself reports (a tray
# of 5 images = 5 jobs, not 1). Planner errors just mean "settings aren't valid yet",
# so we fall back to the raw output count and never block the UI.

def _planned_job_count(prompt, queue, count, media, model_vid, vmode,
                       frame_start, frame_end):
    tmp = Path(".")
    try:
        if media == "video":
            _mode, jobs = plan_video_jobs(prompt or "x", queue, count, "est", tmp,
                                          model_vid, vmode, frame_start, frame_end)
        else:
            _mode, jobs = plan_jobs(prompt or "x", queue, count, "est", tmp)
        return max(1, len(jobs))
    except Exception:
        return max(1, count)


def estimate_cost(prompt, queue, media, model_img, model_vid, size, count,
                  vdur, vmode, frame_start, frame_end):
    """(usd, html) for the pre-flight estimate shown in the composer."""
    try:
        count = max(1, min(int(float(count)), 100))
    except (TypeError, ValueError):
        count = 1
    n = _planned_job_count(prompt, queue or [], count, media, model_vid, vmode,
                           frame_start, frame_end)
    if media == "video":
        m = VIDEO_MODELS.get(model_vid, {})
        secs = snap_dur(model_vid, vdur)
        if m.get("kind") == "omni" and not secs:
            secs = m.get("clip") or 10
        usd = float(m.get("pps", 0.0)) * secs * n
        unit = f"{n} clip{'s' if n != 1 else ''} × {secs}s"
    else:
        usd = PRICE_PER_IMAGE.get(size, 0.067) * n
        unit = f"{n} image{'s' if n != 1 else ''}"
    left = max(0.0, 300.0 - float(load_usage().get("est_cost", 0.0)))
    html_ = (f"<div id='cost-estimate'><b>≈ ${usd:.2f}</b>"
             f"<span class='ce-unit'> · {html.escape(unit)}</span>"
             f"<span class='ce-left'> · ${left:,.0f} left of your $300</span></div>")
    return usd, html_


def cost_estimate_html(prompt, queue, media, model_img, model_vid, size, count,
                       vdur, vmode, frame_start, frame_end):
    return estimate_cost(prompt, queue, media, model_img, model_vid, size, count,
                         vdur, vmode, frame_start, frame_end)[1]


def _note_duration_mismatch(state, path, meta_info):
    """v6.4: say so when Google hands back a clip that isn't the length you asked
    for. Both video paths quietly drop the requested duration and retry when the
    API objects (see gen_video_veo / gen_video_omni), so without this the app just
    produces the wrong length forever and never mentions it — which is exactly how
    the Omni `duration_seconds` bug stayed invisible."""
    if not meta_info or meta_info.get("kind") != "video":
        return
    want = meta_info.get("dur")
    got = _mp4_duration_seconds(path)
    if not want or not got or abs(got - want) < 1:
        return
    with RUNS_LOCK:
        state.setdefault("notes", set()).add(
            f"Google returned ~{round(got)}s clips, not the {int(want)}s you picked — "
            "this model may not accept a custom length.")


def _worker(run_id, client, model, jobs, image_config, workers, price,
            meta_info=None, runner=None):
    state = RUNS[run_id]
    run_fn = runner or generate_one   # videos pass generate_one_video here
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(run_fn, client, model, p, imgs, dst, image_config): dst
                for p, imgs, dst in jobs
            }
            pending = set(futures)
            while pending:
                finished, pending = futures_wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
                for fut in finished:
                    dst = futures[fut]
                    path, e = fut.result()
                    with RUNS_LOCK:
                        state["done"] += 1
                        if path:
                            state["ok"] += 1
                        else:
                            state["errors"].append(f"{dst.name}: {e}")
                    if path:
                        # videos: price is a function of the finished file, so the
                        # tracker bills the seconds Google actually delivered
                        cost = price(Path(path)) if callable(price) else price
                        add_usage(1, cost)
                        if meta_info:
                            record_meta(Path(path), meta_info)
                        _note_duration_mismatch(state, path, meta_info)
                    bump()
    except Exception as e:
        import traceback
        traceback.print_exc()
        with RUNS_LOCK:
            state["errors"].append(f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        with RUNS_LOCK:
            state["finished"] = True
            # keep the registry small: drop oldest finished runs beyond 4
            done_ids = [i for i, r in RUNS.items() if r["finished"]]
            for i in done_ids[:-4]:
                RUNS.pop(i, None)
        bump()


def _start_video_generation(client, queue, prompt, model_label, vmode,
                            frame_start, frame_end, aspect, vres, vdur, vaudio,
                            count, workers, project_name):
    m = VIDEO_MODELS[model_label]
    if aspect == "auto":
        aspect = "16:9"           # a sensible default, not an error
    if aspect not in VIDEO_ASPECTS:
        raise err("Video models only support ▭ 16:9 or ▯ 9:16 — pick one of those "
                  "in the Ratio dropdown (✨ auto counts as 16:9).")
    if vmode != "frames":
        frame_start = frame_end = None   # frame slots only apply in Frames mode
    vdur = snap_dur(model_label, vdur)   # v6.0: never send a length this model refuses
    vcfg = {"aspect": aspect, "res": vres, "dur": int(vdur),
            "audio": bool(vaudio) and m["audio"]}
    if frame_end:
        vcfg["last"] = str(frame_end)

    out_dir = videos_dir(project_name)   # v4.0: videos live in "<Project> (videos)"
    stamp = time.strftime("%H%M%S") + f"{random.randint(0, 99):02d}"
    mode, jobs = plan_video_jobs(prompt, queue, int(count), stamp, out_dir,
                                 model_label, vmode, frame_start, frame_end)

    # cost estimate shown up-front — video is MUCH pricier than images
    if m["kind"] == "omni":
        est_secs = int(vdur) if int(vdur or 0) else (m["clip"] or 10)
    elif m["id"].startswith("veo-2"):
        est_secs = max(5, int(vdur))
    else:
        est_secs = int(vdur)
    est = m["pps"] * est_secs * len(jobs)

    update_config(media="video", model_video=model_label, aspect=aspect,
                  count=int(count), workers=int(workers), vres=vres, vdur=int(vdur),
                  vaudio=bool(vaudio), vmode=vmode, last_project=project_name)

    run_id = next(_run_counter)
    with RUNS_LOCK:
        RUNS[run_id] = {"id": run_id, "mode": mode, "project": project_name,
                        "total": len(jobs), "done": 0, "ok": 0, "errors": [],
                        "started": time.time(), "finished": False}
    meta_info = {"prompt": prompt, "model": model_label, "aspect": aspect,
                 "size": vres, "mode": mode, "kind": "video", "dur": int(vdur),
                 "audio": vcfg["audio"], "vmode": vmode, "ts": int(time.time()),
                 # v6.4: remember what went IN, so ↻ Reuse can restore the frames
                 # and references too — not just the prompt and the settings
                 "frame_start": keep_source(frame_start, project_name) if frame_start else None,
                 "frame_end": keep_source(frame_end, project_name) if frame_end else None,
                 "refs": keep_sources(queue, project_name)}

    pps, fallback_secs = m["pps"], est_secs

    def price(dst):
        return pps * (_mp4_duration_seconds(dst) or fallback_secs)

    threading.Thread(
        target=_worker,
        # video LROs are slow and quota-limited — never more than 2 at once
        args=(run_id, client, model_label, jobs, vcfg,
              max(1, min(int(workers), 2)), price, meta_info),
        kwargs={"runner": generate_one_video},
        daemon=True,
    ).start()
    bump()
    gr.Info(f"Run {run_id} started — {mode}. Estimated ≈ ${est:.2f}. Videos take a "
            "few minutes each and appear in the canvas when ready — you can keep "
            "working meanwhile.", duration=8)
    return build_status(project_name)


def start_generation(queue, prompt, media, model_img_label, model_vid_label,
                     vmode, frame_start, frame_end, aspect, size, count, workers,
                     vres, vdur, vaudio, project, location, project_name):
    if not (prompt or "").strip():
        raise err("Write a prompt first.")

    try:
        count = max(1, min(int(float(count)), 100))   # Outputs is now a free-typed number
    except (TypeError, ValueError):
        count = 1

    client = make_client(project, location)

    if media == "video":
        return _start_video_generation(client, queue, prompt, model_vid_label,
                                       vmode, frame_start, frame_end, aspect,
                                       vres, vdur, vaudio, count, workers,
                                       project_name)

    model_label = model_img_label
    model = MODELS[model_label]

    image_config = {"image_size": size}
    if aspect != "auto":
        image_config["aspect_ratio"] = aspect

    out_dir = images_dir(project_name)   # v4.0: images live in "<Project> (images)"
    stamp = time.strftime("%H%M%S") + f"{random.randint(0, 99):02d}"
    mode, jobs = plan_jobs(prompt, queue, int(count), stamp, out_dir)

    update_config(media="image", model_image=model_label, aspect=aspect, size=size,
                  count=int(count), workers=int(workers), last_project=project_name)

    run_id = next(_run_counter)
    with RUNS_LOCK:
        RUNS[run_id] = {"id": run_id, "mode": mode, "project": project_name,
                        "total": len(jobs), "done": 0, "ok": 0, "errors": [],
                        "started": time.time(), "finished": False}
    meta_info = {"prompt": prompt, "model": model_label, "aspect": aspect,
                 "size": size, "mode": mode, "ts": int(time.time()),
                 "refs": keep_sources(queue, project_name)}   # v6.4: see above
    threading.Thread(
        target=_worker,
        args=(run_id, client, model, jobs, image_config, int(workers),
              PRICE_PER_IMAGE.get(size, 0.067), meta_info),
        daemon=True,
    ).start()
    bump()
    gr.Info(f"Run {run_id} started — {mode}. Images appear as they finish; "
            "you can start another run right away.", duration=5)
    return build_status(project_name)


def runs_in_flight():
    with RUNS_LOCK:
        return any(not r["finished"] for r in RUNS.values())


def poll(project_name, media_filter, seen_version, shown_media):
    """Timer tick: only touch the UI when something actually changed.

    v6.4 — this is the "app keeps refreshing" fix, in two parts.
    1. It now returns the gallery ONLY when the file list genuinely differs from
       what the browser is already showing. Before, any DATA_VERSION bump (a trash
       restore, a usage update, any run event) re-sent the whole list, Gradio
       replaced every tile, and that wiped the `fitApplied` guard in
       enforceCardFit() — so every <video> reloaded and any selection or scroll
       position was lost. That is the flashing/rebuilding Gavin was seeing.
    2. It switches the timer off once nothing is running, so an idle page stops
       polling entirely instead of hitting the share tunnel every 2 seconds.
    """
    with RUNS_LOCK:
        v = DATA_VERSION
    # only ever switch the timer OFF here; it is switched on by whatever starts a
    # run, so we never re-send active=True on every single tick
    timer = gr.update() if runs_in_flight() else gr.update(active=False)
    if seen_version == v:
        return (gr.update(), gr.update(), gr.update(), gr.update(),
                seen_version, shown_media, timer)
    media = project_media(project_name, media_filter)
    same = media == shown_media
    return (gr.update() if same else media, build_status(project_name),
            stat_cards(), top_stats(), v, media, timer)


# ---------------- ingredient tray ----------------

def queue_view(queue):
    items = [(p, f"@{i + 1}") for i, p in enumerate(queue)]
    return gr.update(value=items, visible=len(queue) > 0,
                     label=f"Ingredients — {len(queue)}")


# ---------------- v6.0: one routing rule for EVERY way an image arrives ----------------
# In Frames mode an added image belongs in the frame slots, not the tray: it fills
# Start, then End (when the model supports an end frame), and after that replaces
# Start. Every entry point (＋ Photos, paste, drag-drop, the tile "+" action)
# funnels through here, which is also what makes pasting into the prompt fill the
# frames on a phone — the one paste path iOS reliably allows.

def route_added_images(paths, queue, media, vmode, model_vid, fstart, fend):
    """Returns (queue, fstart, fend, notes[])."""
    paths = [str(p) for p in (paths or []) if p]
    notes = []
    frames_mode = (media == "video" and vmode == "frames")
    if not frames_mode:
        for p in paths:
            if p not in queue:
                queue = queue + [p]
        return queue, fstart, fend, notes
    end_ok = VIDEO_MODELS.get(model_vid, {}).get("kind") != "omni"
    # v6.4: in Frames mode the tray is NEVER touched. Fill Start, then End if this
    # model has one, and once the slots this model supports are full, a further
    # image REPLACES the Start frame. Before, the overflow went to the ingredient
    # tray — so on Omni Flash (no End frame) pasting a second image silently turned
    # it into a prompt reference instead of the frame the user obviously meant.
    for p in paths:
        if not fstart:
            fstart = p
            notes.append(f"Set as the **Start frame** ({Path(p).name}).")
        elif end_ok and not fend:
            fend = p
            notes.append(f"Set as the **End frame** ({Path(p).name}).")
        else:
            fstart = p
            notes.append(f"Replaced the **Start frame** ({Path(p).name}).")
    return queue, fstart, fend, notes


def _routed_updates(queue, fstart, fend, notes):
    """Shared tail for every add path: toast + the four component updates."""
    for n in notes:
        gr.Info(n, duration=4)
    return (queue, queue_view(queue),
            gr.update(value=fstart), gr.update(value=fend))


def add_files(new_files, queue, media, vmode, model_vid, fstart, fend):
    queue, fstart, fend, notes = route_added_images(
        new_files, queue, media, vmode, model_vid, fstart, fend)
    return _routed_updates(queue, fstart, fend, notes)


def add_paste(img_path, queue, media, vmode, model_vid, fstart, fend):
    if not img_path:
        return queue, gr.update(), queue_view(queue), gr.update(), gr.update()
    queue, fstart, fend, notes = route_added_images(
        [img_path], queue, media, vmode, model_vid, fstart, fend)
    q, qv, fs, fe = _routed_updates(queue, fstart, fend, notes)
    return q, None, qv, fs, fe          # None clears the paste tile


def add_pasted_path(store_json, queue, media, vmode, model_vid, fstart, fend):
    """Ctrl+V / drag-and-drop bridge: the browser uploads the image through
    Gradio's upload API, then hands the temp-file path over here."""
    try:
        p = json.loads(store_json or "{}").get("p") or ""
    except Exception:
        p = ""
    path = Path(p) if p else None
    roots = [Path(tempfile.gettempdir()).resolve()]
    if os.environ.get("GRADIO_TEMP_DIR"):
        roots.append(Path(os.environ["GRADIO_TEMP_DIR"]).resolve())
    ok = False
    try:
        ok = bool(path and path.suffix.lower() in IMAGE_EXTS and path.exists()
                  and any(path.resolve().is_relative_to(r) for r in roots))
    except Exception:
        ok = False
    if not ok:
        gr.Warning("Couldn't read that image — try the Add images button instead.",
                   duration=TOAST_SECONDS)
        return queue, queue_view(queue), gr.update(), gr.update()
    queue, fstart, fend, notes = route_added_images(
        [str(path)], queue, media, vmode, model_vid, fstart, fend)
    return _routed_updates(queue, fstart, fend, notes)


def remove_ingredient(store_json, queue):
    """✖ hover button on a tray thumbnail: remove just that ingredient."""
    try:
        idx = int(json.loads(store_json or "{}").get("i", -1))
    except Exception:
        idx = -1
    if 0 <= idx < len(queue):
        queue = queue[:idx] + queue[idx + 1:]
        gr.Info(f"Removed ingredient @{idx + 1} — remaining tags renumbered.", duration=3)
    else:
        gr.Warning("Couldn't find that ingredient anymore.", duration=TOAST_SECONDS)
    return queue, queue_view(queue)


def clear_all():
    """Clear all: empty the ingredient tray AND the prompt for a fresh start."""
    return [], queue_view([]), ""


# ---------------- page load ----------------

def on_page_load():
    cfg = load_config()
    projs = list_projects()
    cur = cfg.get("last_project") if cfg.get("last_project") in projs else UNSORTED
    flt = cfg.get("media_filter") if cfg.get("media_filter") in MEDIA_FILTERS else "All"
    return (stat_cards(), top_stats(),
            gr.update(choices=projs, value=cur),
            gr.update(value=flt),
            project_media(cur, flt), build_status(cur))


# ---------------- UI ----------------

migrate_unsorted()
migrate_media_split()
_migrate_config()
_cfg = load_config()
_projects = list_projects()
_initial_project = _cfg.get("last_project") if _cfg.get("last_project") in _projects else UNSORTED
_initial_filter = _cfg.get("media_filter") if _cfg.get("media_filter") in MEDIA_FILTERS else "All"
_initial_images = project_media(_initial_project, _initial_filter)

# v4.0: image and video models live in SEPARATE dropdowns behind an
# Image/Video toggle. Older configs stored one combined "model_label" —
# map it onto the new keys so nobody's saved choice is lost.
_legacy_model = _cfg.get("model_label")
_initial_img_model = _cfg.get("model_image",
                              _legacy_model if _legacy_model in MODELS else None)
if _initial_img_model not in MODELS:
    _initial_img_model = "Nano Banana 2"
_initial_vid_model = _cfg.get("model_video",
                              _legacy_model if _legacy_model in VIDEO_MODELS else None)
if _initial_vid_model not in VIDEO_MODELS:
    _initial_vid_model = "Veo 3.1 Fast · video"
_initial_media = _cfg.get("media",
                          "video" if _legacy_model in VIDEO_MODELS else "image")
if _initial_media not in ("image", "video"):
    _initial_media = "image"
_initial_vmode = _cfg.get("vmode") if _cfg.get("vmode") in ("frames", "ingredients") else "frames"
_initial_vid = _initial_media == "video"
_im = VIDEO_MODELS.get(_initial_vid_model, {})
_initial_omni = _im.get("kind") == "omni"
_initial_veo2 = _im.get("id", "").startswith("veo-2")

def _adch_build_head():
    """<head> inject for Bananza when served under AdCreateHub: a pending-update
    banner loaded from the hub, plus an inline apple-touch-icon so saving the
    page to an iPhone home screen uses the branded banana glyph."""
    import base64 as _b64
    icon_path = Path(__file__).resolve().parent / "icons" / "icon-180.png"
    icon_tag = ""
    try:
        if icon_path.exists():
            data = _b64.b64encode(icon_path.read_bytes()).decode("ascii")
            # Only the home-screen icon — leave Gradio's own browser-tab favicon alone.
            icon_tag = (
                f'<link rel="apple-touch-icon" sizes="180x180" '
                f'href="data:image/png;base64,{data}">\n'
                '<meta name="apple-mobile-web-app-title" content="Bananza">\n'
                '<meta name="apple-mobile-web-app-capable" content="yes">\n'
                '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n'
                '<meta name="theme-color" content="#f5a623">\n'
            )
    except Exception:
        pass
    pending = """<script>
  (function () {
    var s = document.createElement('script');
    s.src = location.protocol + '//' + location.hostname + ':8000/pending-banner.js';
    s.async = true;
    document.head.appendChild(s);
  })();
</script>"""
    return icon_tag + pending

_ADCH_PENDING_HEAD = _adch_build_head()

with gr.Blocks(title="Bananza 🍌 — Nano Banana bulk generator",
               head=_ADCH_PENDING_HEAD) as demo:

    queue_state = gr.State([])
    result_sel = gr.State(None)
    seen_version = gr.State(0)
    # v6.4: the media list the browser is currently showing, so poll() can tell a
    # real change from a no-op and stop rebuilding the gallery for nothing
    shown_media = gr.State([])

    # ---- top bar (Flow-minimal: logo · project · spend · guide · settings) ----
    with gr.Row(elem_id="topbar"):
        gr.Markdown("<h1 class='app-title'>🍌 Bananza</h1>")
        project_dd = gr.Dropdown(_projects, value=_initial_project, container=False,
                                 interactive=True, scale=0, min_width=190,
                                 elem_id="project-dd")
        top_html = gr.HTML(top_stats(), elem_id="topstats-wrap")
        setup_btn = gr.Button("Guide", size="sm", scale=0, elem_classes=["ico-help"],
                              elem_id="setup-open-btn")
        settings_btn = gr.Button("Settings", size="sm", scale=0, elem_classes=["ico-gear"],
                                 elem_id="settings-open-btn")

    # ---- delete confirmations (floated centered overlays via CSS) ----
    # v6.4: wrapper carries the elem_id that POSITIONS, inner group carries the
    # elem_id that DECORATES — same split as #setup-wrap/#setup-modal. An
    # elem_classes-only card ends up applied to two nested divs, and both going
    # position:fixed is what clipped these dialogs into an unusable strip.
    with gr.Column(visible=False, elem_id="proj-confirm-wrap") as confirm_card:
        with gr.Group(elem_id="proj-confirm-box"):
            confirm_msg = gr.Markdown()
            with gr.Row():
                confirm_yes = gr.Button("Yes, delete it", variant="stop", size="sm", scale=0)
                confirm_no = gr.Button("Cancel", size="sm", scale=0)

    with gr.Column(visible=False, elem_id="img-confirm-wrap") as imgconfirm_card:
        with gr.Group(elem_id="img-confirm-box"):
            imgconfirm_msg = gr.Markdown()
            with gr.Row():
                imgconfirm_yes = gr.Button("Yes, delete them", variant="stop", size="sm", scale=0)
                imgconfirm_no = gr.Button("Cancel", size="sm", scale=0)

    # ---- setup guide modal ----
    with gr.Column(visible=False, elem_id="setup-wrap") as setup_wrap:
        with gr.Group(elem_id="setup-modal"):
            with gr.Row():
                gr.Markdown("### ❓ First time here?")
                close_setup_btn = gr.Button("✕ Close", size="sm", scale=0)
            gr.Markdown(SETUP_GUIDE, elem_id="setup-guide-body")

    # ---- settings + projects modal (was an inline accordion) ----
    with gr.Column(visible=not bool(_cfg.get("project")), elem_id="settings-wrap") as settings_wrap:
        with gr.Group(elem_id="settings-modal"):
            with gr.Row(elem_id="settings-modal-head"):
                gr.Markdown("### ⚙️ Settings & projects")
                close_settings_btn = gr.Button("✕ Close", size="sm", scale=0)
            with gr.Column(elem_id="settings-modal-body"):
                gr.Markdown("#### 📁 Projects")
                with gr.Row():
                    new_project_tb = gr.Textbox(container=False, placeholder="New project name…",
                                                scale=3)
                    create_btn = gr.Button("Create", size="sm", scale=0, elem_classes=["ico-plus"])
                with gr.Row():
                    open_btn = gr.Button("Open folder", size="sm", scale=0,
                                         elem_classes=["ico-folder"])
                    refresh_btn = gr.Button("Refresh", size="sm", scale=0,
                                            elem_classes=["ico-refresh"])
                    del_project_btn = gr.Button("Delete project", size="sm", scale=0,
                                                variant="stop", elem_classes=["trash-btn"])
                gr.Markdown("#### 🔌 Google Cloud connection")
                with gr.Row():
                    project = gr.Textbox(label="Project ID", value=_cfg.get("project", ""),
                                         scale=2,
                                         placeholder="e.g. banana-app-448212 — see the Guide")
                    location = gr.Textbox(label="Location", value=_cfg.get("location", "global"),
                                          scale=1)
                with gr.Row():
                    save_btn = gr.Button("Save", size="sm", scale=0, elem_classes=["ico-save"])
                    test_btn = gr.Button("Test connection", size="sm", scale=0,
                                         elem_classes=["ico-plug"])
                key_status = gr.Markdown(elem_id="key-status")   # v6.0: styled panel
                workers = gr.Slider(
                    1, 8, value=int(_cfg.get("workers", 3)), step=1,
                    label="Images generated at the same time",
                    info="Generating 10 images? At 3, the app asks Google for 3 at once and feeds "
                         "in the rest as each finishes — about 3× faster than one-by-one. Higher "
                         "can be faster still, but Google may refuse with 'rate limit' errors. "
                         "3 is a safe default.")
                # ---- trash manager (v6.0: moved ABOVE the spend tracker and made a
                # proper panel — buried at the bottom as a dark strip it was
                # effectively invisible) ----
                trash_head = gr.Markdown(trash_heading(), elem_id="trash-head")
                gr.Markdown("Deleted items land here. Tap one, then Restore (it goes "
                            "back into the open project) or Delete forever.",
                            elem_classes=["hint-text"])
                with gr.Group(elem_id="trash-panel"):
                    trash_gal = gr.Gallery(value=list_trash(), columns=4, height=240,
                                           object_fit="cover", allow_preview=False,
                                           show_label=False, elem_id="trash-gallery")
                    trash_sel = gr.State(None)
                    with gr.Row():
                        trash_restore_btn = gr.Button("↩ Restore", size="sm", scale=0)
                        trash_purge_btn = gr.Button("Delete forever", size="sm", scale=0,
                                                    variant="stop")
                        trash_empty_btn = gr.Button("Empty trash", size="sm", scale=0)
                    with gr.Row(visible=False) as trash_confirm_row:
                        gr.Markdown("**Permanently delete EVERYTHING in the trash?**")
                        trash_empty_yes = gr.Button("Yes, empty it", variant="stop",
                                                    size="sm", scale=0)
                        trash_empty_no = gr.Button("Cancel", size="sm", scale=0)

                gr.Markdown("#### 💳 Spend tracker")
                usage_html = gr.HTML(stat_cards())
                reset_btn = gr.Button("Reset spend tracker", size="sm")

    # ---- canvas ----
    with gr.Group(elem_id="canvas-card"):
        # height: Gradio writes this string as an INLINE style on .grid-wrap (the
        # scroll container). "58vh" froze the image area at 58% of the window — THE
        # black-rectangle bug: max-height CSS can cap a box but never raise one with
        # an explicit inline height. Full-viewport calc = images reach the bottom edge.
        gallery = gr.Gallery(value=_initial_images, show_label=False,
                             columns=int(_cfg.get("grid", 5)),
                             height="calc(100vh - 70px)",
                             object_fit="contain", elem_id="result-gallery",
                             allow_preview=False)  # viewing = custom JS lightbox
        # Flow-style "View" panel — floats top-right of the canvas, opened by the
        # ⚙ view button (built in FRONTEND_JS). Houses the grid size + media filter.
        with gr.Column(elem_id="view-panel"):
            gr.Markdown("View", elem_id="view-title")
            filter_dd = gr.Dropdown([("🗂 All", "All"), ("🖼 Images", "Images"),
                                     ("🎬 Videos", "Videos")],
                                    value=_initial_filter, container=False,
                                    filterable=False, elem_id="filter-dd")
            grid_dd = gr.Dropdown([("▦ 2", 2), ("▦ 3", 3), ("▦ 4", 4), ("▦ 5", 5),
                                   ("▦ 6", 6), ("▦ 7", 7), ("▦ 8", 8)],
                                  value=int(_cfg.get("grid", 5)), container=False,
                                  filterable=False, elem_id="grid-dd")
        with gr.Row(elem_id="canvas-footer"):
            status = gr.HTML(build_status(_initial_project))
            sel_cap = gr.HTML("")
            del_image_btn = gr.Button("Delete selected", size="sm", scale=0,
                                      elem_id="del-img-btn", elem_classes=["trash-btn"])
        # invisible bridges: browser writes selections / hover-button events here
        multi_sel = gr.Textbox(value="[]", container=False, elem_id="multi-sel-store",
                               elem_classes=["ghost-input"])
        reuse_store = gr.Textbox(value="", container=False, elem_id="reuse-store",
                                 elem_classes=["ghost-input"])
        ing_store = gr.Textbox(value="", container=False, elem_id="ingredient-store",
                               elem_classes=["ghost-input"])
        tray_rm_store = gr.Textbox(value="", container=False, elem_id="tray-remove-store",
                                   elem_classes=["ghost-input"])
        paste_store = gr.Textbox(value="", container=False, elem_id="paste-store",
                                 elem_classes=["ghost-input"])
        # Ghost buttons: JS fills a store above, then clicks one of these.
        # Button clicks are the one frontend event Gradio 6.20 always delivers
        # (synthetic textbox .input events never reach Python listeners).
        reuse_btn = gr.Button("", elem_id="reuse-btn", elem_classes=["ghost-input"], size="sm")
        ing_btn = gr.Button("", elem_id="ingredient-btn", elem_classes=["ghost-input"], size="sm")
        tray_rm_btn = gr.Button("", elem_id="tray-remove-btn", elem_classes=["ghost-input"],
                                size="sm")
        paste_btn = gr.Button("", elem_id="paste-btn", elem_classes=["ghost-input"], size="sm")

    # ---- floating composer (Flow-style centered pill + pop-up settings) ----
    with gr.Group(elem_id="composer"):
        # Pop-up settings panel: floats ABOVE the pill, opened by the model pill in
        # the pill row (toggled in FRONTEND_JS). Holds every generation control.
        with gr.Column(elem_id="settings-pop"):
            # (v4.9: presets removed — every control below saves itself to config.json
            #  the moment it changes, so the app reopens exactly as it was left)
            # v4.0: video-only row — Frames/Ingredients picker + start/end frame slots
            with gr.Row(elem_id="video-row", visible=_initial_vid) as video_row:
                vmode = gr.Radio([("🎞 Frames (start → end)", "frames"),
                                  ("🧺 Ingredients", "ingredients")],
                                 value=_initial_vmode, show_label=False,
                                 elem_id="vmode-radio", scale=0, min_width=300)
                frame_start = gr.Image(sources=["upload", "clipboard"], type="filepath",
                                       label="Start frame", height=104, scale=0,
                                       min_width=160, elem_id="frame-start",
                                       visible=_initial_vid and _initial_vmode == "frames")
                frame_end = gr.Image(sources=["upload", "clipboard"], type="filepath",
                                     label=("End frame — Omni Flash can't use one"
                                            if _initial_omni else "End frame (optional)"),
                                     height=104, scale=0, min_width=160,
                                     elem_id="frame-end",
                                     interactive=not _initial_omni,
                                     visible=_initial_vid and _initial_vmode == "frames")
            with gr.Row(elem_id="settings-row"):
                clear_btn = gr.Button("Clear all (Alt+C)", size="sm", scale=0,
                                      elem_id="clear-all-btn", elem_classes=["eraser-btn"])
                media_type = gr.Radio([("🖼 Image", "image"), ("🎬 Video", "video")],
                                      value=_initial_media, show_label=False,
                                      scale=0, min_width=168, elem_id="media-toggle")
                model_img = gr.Dropdown(list(MODELS), label="Model", scale=2,
                                        filterable=False, value=_initial_img_model,
                                        visible=not _initial_vid, elem_id="model-img")
                model_vid = gr.Dropdown(list(VIDEO_MODELS), label="Model", scale=2,
                                        filterable=False, value=_initial_vid_model,
                                        visible=_initial_vid, elem_id="model-vid")
                aspect = gr.Dropdown(ASPECT_CHOICES, value=_cfg.get("aspect", "auto"),
                                     label="Ratio", scale=1, filterable=False)
                with gr.Column(scale=0, min_width=64):
                    ratio_html = gr.HTML(ratio_preview(_cfg.get("aspect", "auto")))
                size = gr.Dropdown(["1K", "2K", "4K"], value=_cfg.get("size", "1K"),
                                   label="Resolution", scale=1, filterable=False,
                                   visible=not _initial_vid)
                vres = gr.Dropdown(VIDEO_RESOLUTIONS,
                                   value=_cfg.get("vres") if _cfg.get("vres") in VIDEO_RESOLUTIONS else "720p",
                                   label="Resolution", scale=1, filterable=False,
                                   visible=_initial_vid and not _initial_omni and not _initial_veo2)
                # v5.0: Seconds + Sound match composer_visibility so they're correct on
                # FIRST launch, not only after re-picking the model. demo.load(
                # composer_visibility, …) re-asserts it. v6.0: the choices are the
                # lengths THIS model accepts.
                # NOTE: no `filterable=False` here — Gradio forces filterable back on
                # whenever allow_custom_value is True and warns about the conflict.
                # Filterable IS what we want anyway (it's how you type a length).
                vdur = gr.Dropdown(model_durs(_initial_vid_model),
                                   value=snap_dur(_initial_vid_model, _cfg.get("vdur", 8)),
                                   allow_custom_value=True,     # v6.1: type a length too
                                   label="Seconds (pick or type)", scale=1,
                                   visible=_initial_vid)
                vaudio = gr.Checkbox(value=bool(_cfg.get("vaudio", True)), label="Sound",
                                     scale=0, min_width=74,
                                     visible=_initial_vid and bool(_im.get("audio")))
                count = gr.Dropdown(COUNTS, value=int(_cfg.get("count", 1)),
                                    allow_custom_value=True, label="Outputs (pick or type)",
                                    scale=1, elem_id="count-dd")
            # v6.0: what this run will cost, BEFORE you press Generate. JS mirrors
            # this text into the composer pill so it's visible without opening this.
            cost_html = gr.HTML(
                estimate_cost("", [], _initial_media, _initial_img_model,
                              _initial_vid_model, _cfg.get("size", "1K"),
                              _cfg.get("count", 1), _cfg.get("vdur", 8),
                              _initial_vmode, None, None)[1],
                elem_id="cost-wrap")
            gr.Markdown(
                "<div id='mode-hint'>＋ or Ctrl+V adds reference images · @1 @2 combine them · "
                "no images → generate · images → edit each · click a result to view · "
                "drag or Ctrl-click to select<br>"
                "<b>Shortcuts:</b> Enter or Ctrl+Enter generate (Shift+Enter = new line) · "
                "Alt+S settings · Alt+` prompt · Alt+L length · Alt+C clear (Alt+Z undo) · "
                "if the keys ever stop responding, click the page once to give it focus back"
                " · full tips in the Guide · v" + APP_VERSION + "</div>"
            )
        # Ingredient tray — sits just above the pill once you add reference images
        queue_gal = gr.Gallery(visible=False, columns=8, height=104, object_fit="cover",
                               allow_preview=False, show_label=False, label="Ingredients",
                               elem_id="ingredient-tray")
        # The pill itself: ＋ add · prompt · (JS model pill) · → generate
        with gr.Row(elem_id="pill-row"):
            files_in = gr.UploadButton("Add images", file_count="multiple",
                                       file_types=["image"], size="sm", scale=0,
                                       elem_id="add-files-btn",
                                       elem_classes=["ico-imgplus"])
            # clipboard component kept for its paste wiring; the visible tile is
            # hidden — Ctrl+V and drag & drop work anywhere on the page
            paste_in = gr.Image(sources=["clipboard"], type="filepath",
                                show_label=False, height=104, scale=0, min_width=140,
                                elem_id="paste-box")
            prompt = gr.Textbox(show_label=False, lines=1, max_lines=8, scale=8,
                                elem_id="prompt-box",
                                placeholder="What do you want to create?")
            go = gr.Button("→", variant="primary", scale=0, min_width=52,
                           size="lg", elem_id="go-btn")

    # ---- background poller: keeps canvas/stats live while runs stack ----
    # v6.4: starts OFF. Whatever kicks off a run switches it on, and poll() switches
    # it back off when the last run finishes — so an idle page makes no requests at
    # all instead of hitting the share tunnel every 2 seconds forever.
    timer = gr.Timer(2, active=False)
    timer.tick(poll, [project_dd, filter_dd, seen_version, shown_media],
               [gallery, status, usage_html, top_html, seen_version, shown_media, timer],
               show_progress="hidden")

    # ---- wiring ----
    # composer visibility brain (one function, four triggers below + on load) —
    # defined here so the load-time call can reuse it
    _viz_in = [media_type, model_vid, vmode, vdur]
    _viz_out = [model_img, model_vid, size, vres, vdur, vaudio,
                video_row, frame_start, frame_end]
    demo.load(on_page_load, None, [usage_html, top_html, project_dd, filter_dd,
                                   gallery, status])
    # v5.0: derive the composer's initial visibility from the ONE source of truth so
    # a saved Omni Flash model shows Seconds/Sound immediately on first launch
    # (previously the stale creation flags hid them until the model was re-picked).
    demo.load(composer_visibility, _viz_in, _viz_out, show_progress="hidden")
    demo.load(None, None, None, js=FRONTEND_JS)  # dark mode + multi-select wiring
    setup_btn.click(lambda: gr.update(visible=True), None, setup_wrap)
    close_setup_btn.click(lambda: gr.update(visible=False), None, setup_wrap)
    # opening Settings also refreshes the trash gallery so it's always current
    settings_btn.click(lambda: (gr.update(visible=True), gr.update(value=list_trash()),
                                trash_heading()),
                       None, [settings_wrap, trash_gal, trash_head])
    close_settings_btn.click(lambda: gr.update(visible=False), None, settings_wrap)

    # ---- v4.9 trash manager wiring (v6.0: heading count refreshes too) ----
    def on_trash_select(evt: gr.SelectData):
        return evt.index

    trash_gal.select(on_trash_select, None, trash_sel)
    # v6.4: restoring puts a file back INTO the open project, and the main canvas
    # used to pick that up from the 2s poll. The poller is parked while idle now,
    # so refresh the canvas here instead of waiting for a tick that won't come.
    trash_restore_btn.click(restore_trash, [trash_sel, project_dd],
                            [trash_gal, trash_sel]
                            ).then(trash_heading, None, trash_head
                                   ).then(lambda n, flt: (project_media(n, flt), build_status(n)),
                                          [project_dd, filter_dd], [gallery, status],
                                          show_progress="hidden")
    trash_purge_btn.click(purge_trash, trash_sel, [trash_gal, trash_sel]
                          ).then(trash_heading, None, trash_head)
    trash_empty_btn.click(lambda: gr.update(visible=True), None, trash_confirm_row)
    trash_empty_yes.click(lambda: (*empty_trash(), gr.update(visible=False)), None,
                          [trash_gal, trash_sel, trash_confirm_row]
                          ).then(trash_heading, None, trash_head)
    trash_empty_no.click(lambda: gr.update(visible=False), None, trash_confirm_row)
    # v6.0: every add path carries the frame context so Frames mode can route the
    # image into the Start/End slots instead of the tray
    _route_in = [media_type, vmode, model_vid, frame_start, frame_end]
    _route_out = [queue_state, queue_gal, frame_start, frame_end]
    files_in.upload(add_files, [files_in, queue_state] + _route_in, _route_out)
    paste_in.change(add_paste, [paste_in, queue_state] + _route_in,
                    [queue_state, paste_in, queue_gal, frame_start, frame_end])
    clear_btn.click(clear_all, None, [queue_state, queue_gal, prompt])
    save_btn.click(save_settings, [project, location], key_status)
    test_btn.click(test_connection, [project, location], key_status)
    aspect.change(ratio_preview, aspect, ratio_html, show_progress="hidden")
    # one visibility brain, three triggers (toggle / video model / video mode) —
    # plus a .then() after reuse so a restored video flips the composer over too
    # (_viz_in/_viz_out defined above near the demo.load calls)
    media_type.change(composer_visibility, _viz_in, _viz_out, show_progress="hidden")
    model_vid.change(composer_visibility, _viz_in, _viz_out, show_progress="hidden")
    vmode.change(composer_visibility, _viz_in, _viz_out, show_progress="hidden")
    filter_dd.change(change_filter, [filter_dd, project_dd],
                     [gallery, result_sel, sel_cap], show_progress="hidden")
    project_dd.change(switch_project, [project_dd, filter_dd],
                      [gallery, status, result_sel])
    create_btn.click(create_project, [new_project_tb, project_dd, filter_dd],
                     [project_dd, new_project_tb, gallery, status])
    del_project_btn.click(ask_delete_project, project_dd, [confirm_card, confirm_msg])
    confirm_yes.click(confirm_delete_project, [project_dd, filter_dd],
                      [project_dd, gallery, status, confirm_card])
    confirm_no.click(lambda: gr.update(visible=False), None, confirm_card)
    refresh_btn.click(lambda n, flt: (project_media(n, flt), build_status(n)),
                      [project_dd, filter_dd], [gallery, status])
    open_btn.click(open_output_folder, project_dd, None)
    gallery.select(on_result_select, [project_dd, filter_dd], [result_sel, sel_cap])
    grid_dd.change(change_grid, [grid_dd, project_dd], gallery, show_progress="hidden")
    # v6.4: ↻ also puts the Start/End frames and the reference images back, so the
    # last three outputs (and the tray re-render chained after) are new
    reuse_btn.click(reuse_prompt, [reuse_store, project_dd],
                    [prompt, media_type, model_img, model_vid, aspect, size,
                     ratio_html, vres, vdur, vaudio, vmode,
                     frame_start, frame_end, queue_state],
                    show_progress="hidden"
                    ).then(composer_visibility, _viz_in, _viz_out,
                           show_progress="hidden"
                           ).then(queue_view, queue_state, queue_gal,
                                  show_progress="hidden")
    ing_btn.click(add_ingredient, [ing_store, project_dd, queue_state] + _route_in,
                  _route_out, show_progress="hidden")
    tray_rm_btn.click(remove_ingredient, [tray_rm_store, queue_state],
                      [queue_state, queue_gal], show_progress="hidden")
    paste_btn.click(add_pasted_path, [paste_store, queue_state] + _route_in,
                    _route_out, show_progress="hidden")
    del_image_btn.click(delete_images, [multi_sel, result_sel, project_dd, filter_dd],
                        [gallery, status, result_sel, multi_sel,
                         imgconfirm_card, imgconfirm_msg])
    imgconfirm_yes.click(confirm_delete_images, [multi_sel, project_dd, filter_dd],
                         [gallery, status, result_sel, multi_sel, imgconfirm_card])
    imgconfirm_no.click(lambda: gr.update(visible=False), None, imgconfirm_card)
    go.click(start_generation,
             [queue_state, prompt, media_type, model_img, model_vid, vmode,
              frame_start, frame_end, aspect, size, count, workers,
              vres, vdur, vaudio, project, location, project_dd],
             status, show_progress="hidden"
             # v6.4: wake the poller for the duration of this run (poll() parks it
             # again once nothing is in flight)
             ).then(lambda: gr.update(active=runs_in_flight()), None, timer,
                    show_progress="hidden")
    reset_btn.click(reset_tracker, None, [usage_html, top_html])

    # ---- v6.1: a typed Seconds value snaps to what the model actually accepts
    # (so "15" → 10 on Omni, "7" → 6 on Veo 3.x, gibberish → 8). Re-emitting the same
    # value doesn't retrigger .change, so this settles in one pass. ----
    vdur.change(lambda mv, d: gr.update(value=snap_dur(mv, d)), [model_vid, vdur], vdur,
                show_progress="hidden")

    # ---- v4.9: remember-my-settings — every control saves the moment it changes ----
    _state_in = [model_img, model_vid, aspect, size, count, vres, vdur, vaudio]
    for _ctl in _state_in:
        _ctl.change(save_ui_state, _state_in, None, show_progress="hidden")

    # ---- v6.0: keep the pre-flight cost estimate current ----
    _cost_in = [prompt, queue_state, media_type, model_img, model_vid, size, count,
                vdur, vmode, frame_start, frame_end]
    for _ctl in (model_img, model_vid, size, count, vdur, media_type, vmode,
                 frame_start, frame_end, queue_gal):
        _ctl.change(cost_estimate_html, _cost_in, cost_html, show_progress="hidden")
    # v6.4: a Textbox fires .change on EVERY keystroke, so typing a prompt used to
    # queue one server round-trip per character — pure churn over the share tunnel,
    # and it kept re-rendering under the cursor. always_last keeps only the newest.
    prompt.change(cost_estimate_html, _cost_in, cost_html, show_progress="hidden",
                  trigger_mode="always_last")
    demo.load(cost_estimate_html, _cost_in, cost_html, show_progress="hidden")
    # v6.4: if a page opens while a run is already going (reload mid-run, second
    # tab), the poller has to come up running or the canvas would never refresh
    demo.load(lambda: gr.update(active=runs_in_flight()), None, timer,
              show_progress="hidden")


# ---------------- console experience ----------------
# Everything below only runs when you double-click / `python bulk_banana_gui.py`.
# It turns the black window into a friendly loading screen that makes clear the
# window must stay open while the app is used.

_BOX_W = 56


class _Colors:
    """ANSI colors — all empty strings when the console can't display them."""

    def __init__(self, on):
        self.V = "\033[38;5;141m" if on else ""    # violet
        self.P = "\033[38;5;213m" if on else ""    # pink
        self.G = "\033[38;5;114m" if on else ""    # green
        self.Y = "\033[38;5;221m" if on else ""    # yellow
        self.RD = "\033[38;5;203m" if on else ""   # red
        self.D = "\033[2m" if on else ""           # dim
        self.B = "\033[1m" if on else ""           # bold
        self.R = "\033[0m" if on else ""           # reset


def _enable_ansi():
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def _set_console_title():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(
                "Bananza — keep this window open while using the app")
        except Exception:
            pass


def _gradient_text(text, on):
    """Violet→pink gradient across a string (plain text when colors are off)."""
    if not on:
        return text
    ramp = [99, 105, 141, 177, 213, 219]
    n = max(len(text) - 1, 1)
    out = []
    for i, ch in enumerate(text):
        idx = min(round(i / n * (len(ramp) - 1)), len(ramp) - 1)
        out.append(f"\033[38;5;{ramp[idx]}m{ch}")
    return "".join(out) + "\033[0m"


def _box_line(c, plain, colored=None):
    pad = max(_BOX_W - len(plain), 0)
    left = pad // 2
    print(f"  {c.V}║{c.R}{' ' * left}{colored or plain}{' ' * (pad - left)}{c.V}║{c.R}")


def _print_banner(c, ansi_on):
    title = "B A N A N Z A"
    sub = f"Nano Banana  ·  bulk image & video generator  ·  v{APP_VERSION}"
    print()
    print(f"  {c.V}╔{'═' * _BOX_W}╗{c.R}")
    _box_line(c, "")
    _box_line(c, title, c.B + _gradient_text(title, ansi_on) + c.R)
    _box_line(c, sub, f"{c.D}{sub}{c.R}")
    _box_line(c, "")
    print(f"  {c.V}╚{'═' * _BOX_W}╝{c.R}")
    print()
    print(f"  {c.Y}{c.B}■  KEEP THIS WINDOW OPEN{c.R}{c.Y} — it powers the app.{c.R}")
    print(f"     {c.D}Minimize it if it's in the way; closing it stops the app.{c.R}")
    print()


class _Loader:
    """Animated loading bar on one console line (plain prints when not a console)."""

    def __init__(self, colors, tty, message):
        self.c = colors
        self.tty = tty
        self.message = message
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self.tty:
            print(f"  ... {self.message}")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        cells = 16
        t0 = time.time()
        i = 0
        while not self._stop.is_set():
            pos = i % (cells + 6) - 3
            bar = "".join(
                f"{self.c.P}■{self.c.R}" if abs(j - pos) <= 1 else f"{self.c.D}·{self.c.R}"
                for j in range(cells)
            )
            elapsed = int(time.time() - t0)
            sys.stdout.write(f"\r   {bar}  {self.c.V}{self.message}{self.c.R} "
                             f"{self.c.D}({elapsed}s){self.c.R}   ")
            sys.stdout.flush()
            i += 1
            self._stop.wait(0.09)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            sys.stdout.write("\r" + " " * 100 + "\r")
            sys.stdout.flush()


def _print_ready(c, url, share_url=None, share_wanted=False):
    print(f"  {c.G}{c.B}√  The app is running!{c.R}")
    print()
    print(f"     {c.D}Web app{c.R}    {c.B}{url}{c.R}")
    if share_url:
        print(f"     {c.D}Public{c.R}     {c.B}{share_url}{c.R}")
    print(f"     {c.D}Images{c.R}     {OUT_DIR.resolve()}")
    print()
    if share_wanted and not share_url:
        print(f"  {c.RD}Couldn't create the public link (no internet, or a firewall said no).{c.R}")
        print(f"  {c.RD}The normal address above still works on this PC.{c.R}")
        print()
    if share_url:
        print(f"  {c.Y}{c.B}■  PUBLIC LINK IS LIVE{c.R}{c.Y} — open it on any phone or PC.{c.R}")
        print(f"     {c.D}Anyone with that link can generate images using YOUR credits,{c.R}")
        print(f"     {c.D}so only share it with people you trust. It stops working when{c.R}")
        print(f"     {c.D}this window closes.{c.R}")
        print()
    print(f"  {c.Y}{c.B}■  KEEP THIS WINDOW OPEN{c.R}{c.Y} — it powers the app.{c.R}")
    print(f"     {c.D}Minimize it if it's in the way.{c.R}")
    print()
    if not share_url:
        print(f"     {c.D}Want a public link (use it on your phone / share with a friend)?{c.R}")
        print(f"     {c.D}Close this window, then run:{c.R}  {c.B}python bulk_banana_gui.py --share{c.R}")
        print(f"     {c.D}Heads-up: whoever has the link generates with YOUR credits.{c.R}")
        print()
    print(f"     {c.D}Browser didn't open by itself? Copy the address above{c.R}")
    print(f"     {c.D}into Chrome or Edge.{c.R}")
    print(f"     {c.D}Finished for today? Just close this window.{c.R}")
    print()
    print(f"  {c.V}{'═' * (_BOX_W + 2)}{c.R}")
    print()


# ---------------- v6.3: stop re-downloading media on every page load ----------------
# THE buffering culprit: Gradio serves files from /gradio_api/file=… with an ETag and
# Last-Modified but NO Cache-Control, so the browser re-fetched every video and every
# image on each page load (measured: 3 clips = 617KB pulled again on a warm reload,
# and Gavin's real clips are megabytes over a gradio.live tunnel).
#
# Generated files are write-once — a new generation always gets a new filename — so
# they're safe to cache hard. A week's max-age kills the repeat downloads while still
# letting a stale entry age out, and no `immutable` means Ctrl+F5 can always refetch.
# Middleware has to be installed when the FastAPI app is CONSTRUCTED (Starlette locks
# the stack at startup), which is exactly what launch(app_kwargs=…) allows.

_CACHEABLE_EXTS = MEDIA_EXTS + (".webp",)


def _media_cache_app_kwargs():
    try:
        from starlette.middleware import Middleware
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import RedirectResponse
    except Exception:
        return {}                      # no Starlette? just run without the optimisation

    class MediaCacheMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            # v6.4: bounce a bare "/" to the dark URL BEFORE the app loads.
            # The frontend used to do this itself with window.location.href, which
            # meant the whole app loaded, then threw itself away and loaded again —
            # a real page reload on every visit from a bookmark or a share link,
            # taking the canvas and anything unsaved in the composer with it.
            # Redirecting here costs one cheap HTTP hop and the app loads once.
            # v6.5 correction: an earlier comment here claimed this redirect was the
            # ONLY way to get dark mode. That was wrong — it came from measuring
            # document.body (always light; the visible surface is .gradio-container).
            # Measured properly: the page renders dark either way, and the `dark`
            # class on body is what drives Gradio's component palette. The redirect
            # is kept because it makes Gradio agree with us instead of racing us on
            # its own theme handling — belt and braces, not the mechanism.
            try:
                if request.url.path == "/" and "__theme" not in request.query_params:
                    q = str(request.url.query)
                    return RedirectResponse("/?" + (q + "&" if q else "") + "__theme=dark",
                                            status_code=307)
            except Exception:
                pass
            response = await call_next(request)
            try:
                path = request.url.path.lower()
                if "file=" in path and path.endswith(_CACHEABLE_EXTS):
                    response.headers["Cache-Control"] = "public, max-age=604800"
            except Exception:
                pass
            return response

    return {"middleware": [Middleware(MediaCacheMiddleware)]}


def main():
    no_browser = "--no-browser" in sys.argv or bool(os.environ.get("BANANA_NO_BROWSER"))
    share = "--share" in sys.argv or bool(os.environ.get("BANANA_SHARE"))
    host = os.environ.get("BANANA_HOST") or None      # e.g. 0.0.0.0 on a server
    try:
        port = int(os.environ.get("BANANA_PORT") or 0) or None
    except ValueError:
        port = None
    auth = os.environ.get("BANANA_AUTH") or ""        # "user:pass" for a login gate
    tty = bool(sys.stdout) and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    ansi_on = tty and _enable_ansi()
    c = _Colors(ansi_on)
    _set_console_title()
    if tty:
        os.system("cls" if os.name == "nt" else "clear")
    _print_banner(c, ansi_on)

    loader = _Loader(c, tty, "Starting the app"
                     + (" + creating your public link" if share else "")
                     + " — your browser will open by itself…")
    loader.start()
    try:
        launch_kwargs = dict(
            inbrowser=not no_browser,
            share=share,
            show_error=True,
            allowed_paths=[str(OUT_DIR.resolve())],
            app_kwargs=_media_cache_app_kwargs(),   # v6.3: stop re-downloading media
            css=CSS,
            js=FRONTEND_JS,
            theme=gr.themes.Soft(
                primary_hue="violet",
                neutral_hue="zinc",
                font=[gr.themes.GoogleFont("Inter"), "ui-sans-serif", "system-ui", "sans-serif"],
                font_mono=[gr.themes.GoogleFont("JetBrains Mono"), "ui-monospace", "monospace"],
            ),
            prevent_thread_lock=True,
        )
        if host:
            launch_kwargs["server_name"] = host
        if port:
            launch_kwargs["server_port"] = port
        if ":" in auth:
            launch_kwargs["auth"] = tuple(auth.split(":", 1))
        try:
            if "quiet" in inspect.signature(demo.launch).parameters:
                launch_kwargs["quiet"] = True
        except Exception:
            pass
        demo.launch(**launch_kwargs)
    finally:
        loader.stop()

    url = (getattr(demo, "local_url", None) or "http://127.0.0.1:7860").rstrip("/")
    share_url = (getattr(demo, "share_url", None) or "").rstrip("/") or None
    _print_ready(c, url, share_url, share_wanted=share)

    try:
        demo.block_thread()
    except AttributeError:
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
        print("\n  Bye! The app has shut down — you can close this window.")
    except KeyboardInterrupt:
        print("\n  Bye! The app has shut down — you can close this window.")
    except Exception:
        import traceback
        traceback.print_exc()
        _c = _Colors(False)
        print()
        print("  Something went wrong while starting the app (details above).")
        print("  Take a photo/screenshot of this window if you need help.")
        try:
            if sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
                input("\n  Press Enter to close this window... ")
        except Exception:
            pass
