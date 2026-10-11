"""Grid thumbnails, video posters and low-res hover previews.

Everything lives in <project>/.thumbs/ (and output/.trash/.thumbs/) and is
rebuilt on demand — the folders are safe to delete at any time.

Staleness rule: a cache file older than its source is deleted and rebuilt.
Regenerate reuses filenames, so without this an item would keep its old thumb.
"""
import os
import queue
import subprocess
import threading
import traceback

from .config import TRASH_DIR, load_config
from .models_tables import VIDEO_EXTS
from .projects import thumbs_dir, find_media, trash_path

THUMB_MAX = 480
_ffmpeg_exe = None
_ffmpeg_lock = threading.Lock()

# One lock per cache file, not one global lock: the grid asks for ~40 thumbs at
# once and they must build in parallel, while the same file never builds twice.
_state_lock = threading.Lock()
_build_locks = {}

_preview_q = queue.Queue()
_preview_seen = set()
_preview_thread = None


def _lock_for(key):
    with _state_lock:
        lk = _build_locks.get(key)
        if lk is None:
            if len(_build_locks) > 2048:
                _build_locks.clear()     # bounded: worst case is one extra build
            lk = threading.Lock()
            _build_locks[key] = lk
        return lk


def _ffmpeg():
    """The static ffmpeg binary pip installs with imageio-ffmpeg — never a system one."""
    global _ffmpeg_exe
    with _ffmpeg_lock:
        if _ffmpeg_exe is None:
            try:
                import imageio_ffmpeg
                _ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                traceback.print_exc()
                _ffmpeg_exe = ""
        return _ffmpeg_exe or None


def _run(args):
    exe = _ffmpeg()
    if not exe:
        return False
    kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000        # CREATE_NO_WINDOW
    try:
        return subprocess.call([exe, "-y", "-loglevel", "error"] + args, **kwargs) == 0
    except Exception:
        traceback.print_exc()
        return False


def _fresh(cache, src):
    """True when the cache file exists and is at least as new as its source."""
    try:
        if not cache.exists():
            return False
        if cache.stat().st_mtime < src.stat().st_mtime:
            cache.unlink(missing_ok=True)
            return False
        return cache.stat().st_size > 0
    except OSError:
        return False


# ---------------- builders ----------------

def _build_image_thumb(src, dest):
    try:
        from PIL import Image
        with Image.open(src) as im:
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            im.thumbnail((THUMB_MAX, THUMB_MAX), Image.LANCZOS)
            im.save(dest, "WEBP", quality=78, method=4)
        return True
    except Exception:
        traceback.print_exc()
        return False


def _build_poster(src, dest):
    return _run(["-ss", "0", "-i", str(src), "-frames:v", "1",
                 "-vf", "scale=480:-2", "-q:v", "4", str(dest)])


def _build_preview(src, dest):
    return _run(["-i", str(src), "-vf", "scale=360:-2", "-an",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
                 "-movflags", "+faststart", str(dest)])


# ---------------- project cache ----------------

def thumb_file(project, name):
    return thumbs_dir(project) / f"{name}.thumb.webp"


def poster_file(project, name):
    return thumbs_dir(project) / f"{name}.poster.jpg"


def preview_file(project, name):
    return thumbs_dir(project) / f"{name}.preview.mp4"


def ensure_thumb(project, name):
    """Path to the grid image for `name` (thumb for images, poster for videos),
    building it synchronously if it's missing or stale. None if it can't be made."""
    src = find_media(project, name)
    if not src:
        return None
    is_video = src.suffix.lower() in VIDEO_EXTS
    dest = poster_file(project, src.name) if is_video else thumb_file(project, src.name)
    if _fresh(dest, src):
        return dest
    with _lock_for(str(dest)):
        if _fresh(dest, src):
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        ok = _build_poster(src, dest) if is_video else _build_image_thumb(src, dest)
    return dest if ok and dest.exists() else None


def preview_ready(project, name):
    src = find_media(project, name)
    return bool(src) and preview_ready_for(project, src)


def preview_ready_for(project, src):
    """Same check when the caller already has the source path (grid listing)."""
    return _fresh(preview_file(project, src.name), src)


def get_preview(project, name):
    """Previews are never built synchronously — the grid keeps showing the poster
    until the background thread catches up (§7)."""
    src = find_media(project, name)
    if not src:
        return None
    dest = preview_file(project, src.name)
    return dest if _fresh(dest, src) else None


def queue_preview(project, name):
    if not load_config().get("hover_previews", True):
        return
    key = (project, name)
    with _state_lock:
        if key in _preview_seen:
            return
        _preview_seen.add(key)
    _preview_q.put(key)
    _start_preview_thread()


def _preview_worker():
    while True:
        project, name = _preview_q.get()
        try:
            src = find_media(project, name)
            if src and not _fresh(preview_file(project, src.name), src):
                dest = preview_file(project, src.name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not _build_preview(src, dest):
                    dest.unlink(missing_ok=True)   # fall back to the poster
                else:
                    from .gen_engine import bump
                    bump()
        except Exception:
            traceback.print_exc()
        finally:
            with _state_lock:
                _preview_seen.discard((project, name))
            _preview_q.task_done()


def _start_preview_thread():
    global _preview_thread
    with _state_lock:
        if _preview_thread and _preview_thread.is_alive():
            return
        _preview_thread = threading.Thread(target=_preview_worker, daemon=True,
                                           name="megagen-previews")
        _preview_thread.start()


def build_after_generation(project, name, kind):
    """Eager, best-effort: called in the worker thread the moment a file lands."""
    try:
        ensure_thumb(project, name)
        if kind == "video":
            queue_preview(project, name)
    except Exception:
        traceback.print_exc()


def drop_cache(project, name):
    """Forget every cached artifact for one file (used when it leaves the project)."""
    for f in (thumb_file(project, name), poster_file(project, name),
              preview_file(project, name)):
        try:
            f.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------- trash cache ----------------

def _trash_thumbs_dir():
    d = TRASH_DIR / ".thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_trash_thumb(name):
    src = trash_path(name)
    if not src:
        return None
    is_video = src.suffix.lower() in VIDEO_EXTS
    dest = _trash_thumbs_dir() / (f"{src.name}.poster.jpg" if is_video
                                  else f"{src.name}.thumb.webp")
    if _fresh(dest, src):
        return dest
    with _lock_for(str(dest)):
        if _fresh(dest, src):
            return dest
        ok = _build_poster(src, dest) if is_video else _build_image_thumb(src, dest)
    return dest if ok and dest.exists() else None


def drop_trash_thumb(name):
    d = TRASH_DIR / ".thumbs"
    if not d.is_dir():
        return
    for suffix in (".poster.jpg", ".thumb.webp"):
        try:
            (d / f"{name}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass


def clear_trash_thumbs():
    d = TRASH_DIR / ".thumbs"
    if not d.is_dir():
        return
    for f in list(d.iterdir()):
        try:
            if f.is_file():
                f.unlink(missing_ok=True)
        except OSError:
            pass


def cache_path_for(project, name, what):
    """Router helper for the /thumb and /preview endpoints."""
    if what == "preview":
        return get_preview(project, name)
    return ensure_thumb(project, name)
