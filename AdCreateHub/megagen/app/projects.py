"""Project folders, media listing, the per-project meta store, and the trash.

Ported from bulk_banana_gui.py: sanitize_name, the HHMMSS_ trash naming scheme,
_trash_orig_name's two branches, restore-collision naming, open_output_folder's
platform branching, and the locked .meta.json load/save pattern.
"""
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .config import OUT_DIR, TRASH_DIR, DEFAULT_PROJECT, ensure_dirs, notice
from .models_tables import IMAGE_EXTS, VIDEO_EXTS, MEDIA_EXTS

# Both are re-entrant: the write helpers read the current file while holding the
# lock, so a plain Lock would deadlock against itself.
_meta_lock = threading.RLock()
_trashmeta_lock = threading.RLock()
_meta_cache = {}      # str(.meta.json path) -> (mtime_ns, size, parsed dict)

SUBDIRS = ("refs", "frames", "images", "videos", ".thumbs", ".inputs")


def sanitize_name(name):
    return re.sub(r"[^\w\- ]", "", (name or "")).strip()[:60]


# ---------------- folders ----------------

_ensured = set()


def project_dir(name):
    """The project folder, with its subfolders guaranteed to exist. The mkdir
    storm only runs once per project per session — /api/state walks this path
    for every listed item, so six syscalls per call would be felt at 300+ tiles."""
    safe = sanitize_name(name) or DEFAULT_PROJECT
    d = OUT_DIR / safe
    if safe in _ensured and d.is_dir():
        return d
    ensure_dirs()
    d.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        (d / sub).mkdir(exist_ok=True)
    _ensured.add(safe)
    return d


def refs_dir(name):
    return project_dir(name) / "refs"


def frames_dir(name):
    return project_dir(name) / "frames"


def images_dir(name):
    return project_dir(name) / "images"


def videos_dir(name):
    return project_dir(name) / "videos"


def thumbs_dir(name):
    return project_dir(name) / ".thumbs"


def inputs_dir(name):
    """Private copies of references/start frames that generated media still needs.
    Deleting a ref from the panel must never cost an item its ability to replay."""
    return project_dir(name) / ".inputs"


def media_dir(name, kind):
    return videos_dir(name) if kind == "video" else images_dir(name)


def list_projects():
    ensure_dirs()
    subs = sorted([d.name for d in OUT_DIR.iterdir()
                   if d.is_dir() and d.name != ".trash"], key=str.lower)
    if DEFAULT_PROJECT not in subs:
        project_dir(DEFAULT_PROJECT)
        subs = sorted(subs + [DEFAULT_PROJECT], key=str.lower)
    return subs


def valid_project(name):
    """Resolve a requested project name to a real folder, never escaping output/."""
    safe = sanitize_name(name)
    if safe and (OUT_DIR / safe).is_dir():
        return safe
    return DEFAULT_PROJECT


def create_project(name):
    safe = sanitize_name(name)
    if not safe:
        raise ValueError("Type a name for the new project first.")
    if (OUT_DIR / safe).is_dir():
        raise ValueError(f"A project called “{safe}” already exists.")
    project_dir(safe)
    return safe


def delete_project(name):
    safe = sanitize_name(name)
    if safe == DEFAULT_PROJECT:
        raise ValueError("The Default project can't be deleted.")
    src = OUT_DIR / safe
    if not src.is_dir():
        raise ValueError("That project doesn't exist.")
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(TRASH_DIR / f"{src.name}_{time.strftime('%Y%m%d_%H%M%S')}"))
    return safe


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
        return f"Opened the '{name}' folder."
    except Exception:
        return f"Folder: {d.resolve()}"


# ---------------- meta store (one .meta.json per project) ----------------

def meta_path(name):
    return project_dir(name) / ".meta.json"


def load_meta(name):
    with _meta_lock:
        p = meta_path(name)
        key = str(p)
        try:
            st = p.stat()
        except OSError:
            _meta_cache.pop(key, None)
            return {}
        hit = _meta_cache.get(key)
        if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return dict(hit[2])          # shallow copy: callers add/pop keys
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            m = m if isinstance(m, dict) else {}
        except Exception:
            m = {}
        _meta_cache[key] = (st.st_mtime_ns, st.st_size, m)
        return dict(m)


def _write_meta(name, m):
    try:
        p = meta_path(name)
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        _meta_cache.pop(str(p), None)    # next load re-stats and re-caches
    except Exception:
        notice(f"Couldn't write .meta.json for “{name}” — Regenerate/New won't work "
               "for new items until that's fixed.")


def record_meta(name, basename, info):
    with _meta_lock:
        m = load_meta(name)
        m[Path(basename).name] = info
        _write_meta(name, m)


def pop_meta(name, basename):
    with _meta_lock:
        m = load_meta(name)
        info = m.pop(Path(basename).name, None)
        if info is not None:
            _write_meta(name, m)
        return info


def meta_for(name, basename):
    return load_meta(name).get(Path(basename).name)


# ---------------- preserved inputs (refs / start frames) ----------------

def input_path(project, name, kind):
    """Resolve a recipe's reference/frame filename to a real file: the live
    folder first, then the copy preserved when the user deleted it."""
    if not name:
        return None
    safe = Path(str(name)).name
    live = (frames_dir(project) if kind == "frames" else refs_dir(project)) / safe
    if live.exists() and live.is_file():
        return live
    kept = inputs_dir(project) / kind / safe
    if kept.exists() and kept.is_file():
        return kept
    return None


def input_is_used(project, name, kind):
    """True when at least one generated item's recipe still names this file."""
    safe = Path(str(name)).name
    for info in load_meta(project).values():
        if not isinstance(info, dict):
            continue
        if kind == "frames":
            if info.get("frame") == safe:
                return True
        elif safe in (info.get("refs") or []):
            return True
    return False


def preserve_input(project, path, kind):
    """Called just before a ref/frame is deleted: if generated media still points
    at it, keep a private copy so Regenerate/New/Edit replay with the ORIGINAL
    image even after the user clears the folder."""
    p = Path(path)
    try:
        if not p.is_file() or not input_is_used(project, p.name, kind):
            return False
        dest_dir = inputs_dir(project) / kind
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / p.name
        if not dest.exists():
            shutil.copyfile(p, dest)
        return True
    except OSError:
        return False        # a copy we couldn't make must not block the delete


# ---------------- media listing ----------------

def find_media(project, basename):
    """Locate one generated file by basename. Basename only — path escapes are
    impossible by construction (ported find_media philosophy)."""
    if not basename:
        return None
    safe = Path(str(basename)).name
    ext = Path(safe).suffix.lower()
    dirs = []
    if ext in VIDEO_EXTS:
        dirs = [videos_dir(project)]
    elif ext in IMAGE_EXTS:
        dirs = [images_dir(project)]
    else:
        return None
    for d in dirs:
        p = d / safe
        if p.exists() and p.is_file():
            return p
    return None


_LEADING_N = re.compile(r"^(\d{1,4})")


def _n_from_name(basename):
    m = _LEADING_N.match(basename)
    return int(m.group(1)) if m else 0


def list_media(project, tab, thumbs_mod=None):
    """The grid's data for one tab, sorted (n asc, seq asc, ts asc, name asc)."""
    kind = "video" if tab == "videos" else "image"
    d = media_dir(project, kind)
    want = VIDEO_EXTS if kind == "video" else IMAGE_EXTS
    meta = load_meta(project)
    out = []
    if not d.is_dir():
        return out
    for f in d.iterdir():
        if not (f.is_file() and f.suffix.lower() in want):
            continue          # junk files are simply ignored (§10.12)
        info = meta.get(f.name) or {}
        try:
            st = f.stat()
        except OSError:
            continue
        item = {
            "name": f.name,
            "kind": kind,
            "n": int(info.get("n", _n_from_name(f.name))),
            "seq": float(info.get("seq", 0.0)),
            "ts": int(info.get("ts", st.st_mtime)),
            "mtime": int(st.st_mtime),
            "meta": info or None,
        }
        if kind == "video":
            item["dur"] = info.get("dur")
            item["has_preview"] = bool(thumbs_mod
                                       and thumbs_mod.preview_ready_for(project, f))
        out.append(item)
    out.sort(key=lambda i: (i["n"], i["seq"], i["ts"], i["name"]))
    return out


def unique_media_name(project, n, kind, rng, taken=()):
    """`7-gav482913.png` / `7Vid-gav118266.mp4` — re-rolled while the name exists
    in the media folder, the trash, .meta.json, or another job's plan (§10.1)."""
    ext = ".mp4" if kind == "video" else ".png"
    tag = "Vid" if kind == "video" else ""
    d = media_dir(project, kind)
    meta = load_meta(project)
    trash_names = set()
    if TRASH_DIR.exists():
        trash_names = {p.name for p in TRASH_DIR.iterdir() if p.is_file()}
    for _ in range(2000):
        cand = f"{n}{tag}-gav{rng.randint(0, 999999):06d}{ext}"
        if cand in taken or (d / cand).exists() or cand in meta:
            continue
        if any(t.endswith("_" + cand) for t in trash_names):
            continue
        return cand
    raise RuntimeError("Couldn't find a free output filename")


# ---------------- trash ----------------

def _unlink_retry(path, attempts=6, delay=0.25, _sleep=time.sleep):
    """Delete one file, retrying briefly when Windows says it's still in use
    (WinError 32 — an ffmpeg thumbnail build or an open media stream can hold
    a file for a moment). `path` is a pathlib.Path (or anything with .unlink()).
    True = the file is gone; False = still locked after every attempt."""
    for i in range(attempts):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except (PermissionError, OSError):
            if i == attempts - 1:
                return False
            _sleep(delay)
    return False


def _trash_meta_file():
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    return TRASH_DIR / ".trashmeta.json"


def load_trashmeta():
    with _trashmeta_lock:
        p = _trash_meta_file()
        if p.exists():
            try:
                m = json.loads(p.read_text(encoding="utf-8"))
                return m if isinstance(m, dict) else {}
            except Exception:
                pass
        return {}


def _write_trashmeta(m):
    try:
        _trash_meta_file().write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _record_trashmeta(trash_name, orig, project, meta):
    with _trashmeta_lock:
        m = load_trashmeta()
        m[trash_name] = {"orig": orig, "project": project, "meta": meta}
        _write_trashmeta(m)


def _pop_trashmeta(trash_name):
    with _trashmeta_lock:
        m = load_trashmeta()
        info = m.pop(trash_name, None)
        if info is not None:
            _write_trashmeta(m)
        return info


def trash_file(project, path):
    """Move one generated file to the trash, carrying its recipe along (§4.4)."""
    TRASH_DIR.mkdir(parents=True, exist_ok=True)
    p = Path(path)
    base = f"{time.strftime('%H%M%S')}_{p.name}"
    dest = TRASH_DIR / base
    k = 1
    while dest.exists():                      # same name, same second — stay unique
        k += 1
        dest = TRASH_DIR / f"{time.strftime('%H%M%S')}_{p.stem} ({k}){p.suffix}"
    info = pop_meta(project, p.name)
    shutil.move(str(p), str(dest))
    _record_trashmeta(dest.name, p.name, project, info)
    return dest.name


def trash_orig_name(fname):
    """Undo either trash-naming scheme: 'HHMMSS_name.png' or 'name.png_YYYYMMDD_HHMMSS'."""
    m = re.match(r"^\d{6}_(.+)$", fname)
    if m:
        return m.group(1)
    m = re.match(r"^(.+?)_\d{8}_\d{6}$", fname)
    if m:
        return m.group(1)
    return fname


def list_trash():
    """Trashed media, newest first."""
    if not TRASH_DIR.exists():
        return []
    tm = load_trashmeta()
    files = [p for p in TRASH_DIR.iterdir()
             if p.is_file() and p.suffix.lower() in MEDIA_EXTS]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    out = []
    for p in files:
        rec = tm.get(p.name) or {}
        orig = rec.get("orig") or trash_orig_name(p.name)
        out.append({
            "name": p.name,
            "orig": orig,
            "kind": "video" if p.suffix.lower() in VIDEO_EXTS else "image",
            "ts": int(p.stat().st_mtime),
            "project": rec.get("project") or "",
        })
    return out


def trash_count():
    if not TRASH_DIR.exists():
        return 0
    return sum(1 for p in TRASH_DIR.iterdir()
               if p.is_file() and p.suffix.lower() in MEDIA_EXTS)


def trash_path(name):
    safe = Path(str(name)).name
    p = TRASH_DIR / safe
    if p.exists() and p.is_file() and p.suffix.lower() in MEDIA_EXTS:
        return p
    return None


def restore_trash(name, project):
    """Back into the OPEN project, images→images/, videos→videos/ (ported rule)."""
    src = trash_path(name)
    if not src:
        raise ValueError("That item isn't in the trash anymore.")
    rec = load_trashmeta().get(src.name) or {}
    orig = rec.get("orig") or trash_orig_name(src.name)
    kind = "video" if Path(orig).suffix.lower() in VIDEO_EXTS else "image"
    dest_dir = media_dir(project, kind)
    dest = dest_dir / orig
    k = 1
    while dest.exists():                       # collision-safe: name (2).png, …
        k += 1
        dest = dest_dir / f"{Path(orig).stem} ({k}){Path(orig).suffix}"
    shutil.move(str(src), str(dest))
    info = rec.get("meta")
    if info:
        record_meta(project, dest.name, info)
    _pop_trashmeta(src.name)
    return dest.name


def purge_trash(name):
    src = trash_path(name)
    if not src:
        raise ValueError("That item isn't in the trash anymore.")
    if not _unlink_retry(src):
        raise ValueError("Windows still has this file open (a preview may be "
                         "building or playing) — wait a moment and try again.")
    _pop_trashmeta(src.name)
    from . import thumbs
    thumbs.drop_trash_thumb(src.name)
    return True


def empty_trash():
    """Delete everything deletable. Returns (purged_count, locked_names).
    A file Windows still holds open is skipped and reported — never a crash —
    and its trash-meta entry survives so a later attempt can still restore or
    purge it properly."""
    purged, locked = [], []
    if TRASH_DIR.exists():
        for p in list(TRASH_DIR.iterdir()):
            if not (p.is_file() and p.suffix.lower() in MEDIA_EXTS):
                continue
            if _unlink_retry(p):
                purged.append(p.name)
            else:
                locked.append(p.name)
    with _trashmeta_lock:
        m = load_trashmeta()
        for nm in purged:
            m.pop(nm, None)
        _write_trashmeta(m)
    from . import thumbs
    thumbs.clear_trash_thumbs()
    return len(purged), locked
