# MEGAGEN v1.1 — Change Specification for Claude Code
## 10 user-facing changes + 4 internal fixes. Every decision has already been made.

---

## §0. OPERATING RULES FOR CLAUDE CODE — READ FIRST

1. **Every decision has been made.** This document tells you exactly what to build, where, and how — including the exact code. Your job is implementation. Do not redesign, rename, simplify, "improve", or add anything beyond this spec.
2. **Ask the user only if** (a) two parts of this spec directly contradict each other, or (b) an anchor string quoted below genuinely does not exist in the codebase. Everything else: build as written. The anchors in this document were verified against the current source character-for-character.
3. **Anchors.** Every edit below quotes the CURRENT code as `OLD` and the replacement as `NEW`. `OLD` blocks are verbatim excerpts — match them exactly (whitespace included). When a section says "insert after/before", the anchor line(s) are quoted verbatim too.
4. **Line endings.** These files use CRLF: `app/gen_engine.py`, `app/thumbs.py`, `static/app.js`. Everything else uses LF. Match whatever the file you are editing already uses; never convert a whole file.
5. **Zero new dependencies.** `requirements.txt` is unchanged. No frameworks, no build step, vanilla JS only, styling only through the existing CSS custom properties.
6. **Reuse existing machinery.** The run registry, `bump()`/version polling, `notice()`, the toast/confirm helpers, `_send()` Range serving, and the `.lb-*` visual language are the house style. Do not invent parallel systems.
7. **Do not touch** anything listed in §13 (Regression Contract). `bulk_banana_gui.py` and `run_megagen.py` are not modified at all.
8. When done, verify every line of §14 (Definition of Done) in order.

---

## §1. CHANGE INDEX — what and why

| ID | Change | Root cause (already diagnosed — do not re-investigate) |
|----|--------|--------------------------------------------------------|
| C1 | Tile action buttons stay visible over a playing hover preview | `grid.js` appends the `<video.tp>` element LAST inside `.tile`, and no overlay (`.tile-acts`, `.tile-badge`, `.tile-dur`, `.tile-play`, `.tile-busy`) has a `z-index` — so DOM order paints the video on top. Fix = explicit stacking in CSS only. No JS change. |
| C2 | Every error message becomes plain English | Raw Google/SDK exception text (JSON walls, gRPC codes) is appended verbatim to `state["errors"]` in `_run_worker`. Fix = new `app/errors.py` with `humanize()`, applied at the single point where errors are appended. Raw text still goes to the terminal via the existing `traceback.print_exc()` calls. |
| C3 | "Empty trash says a file is still in use" | `purge_trash`/`empty_trash` call `p.unlink(missing_ok=True)`. `missing_ok` only suppresses `FileNotFoundError`; on Windows a file held open by an ffmpeg thumbnail build or a streaming response raises `PermissionError` (WinError 32), which propagates and 500s the endpoint mid-loop. Fix = `_unlink_retry()` helper (brief retries), skip-and-report instead of crash, and the new trash viewer (C4) releases its file handle before any purge. |
| C4 | Fullscreen preview of items in the trash | No endpoint serves full trash media and no viewer exists. Fix = `GET /trash-media/{name}` (reuses `_send`) + a dedicated `#trash-view` overlay (double-click a trash tile), with ←/→ navigation, restore/purge buttons, layered ABOVE the settings modal. It deliberately uses the plain `<video controls>` — the custom player (C8) is for the main lightbox only. |
| C5 | Del key confirms the delete-confirmation dialog | No keyboard handling exists on `#confirm-modal`. Fix = capture-phase key handler: `Delete` confirms, `Escape` cancels, held-key auto-repeat (`e.repeat`) is ignored so holding Del cannot blow through the dialog. |
| C6 | Copy image actually copies | The current code re-encodes through a canvas and hands `clipboard.write()` a **Promise** for the PNG. Chrome resolves the write before that promise settles, so a slow/failed encode shows "Image copied" while the clipboard stays empty/incomplete. All generated images are PNG on disk (`projects.unique_media_name` → `.png`), so the canvas is pointless anyway. Fix = fetch the original PNG bytes, materialise the Blob FIRST, then write. |
| C7 | "Multiple outputs → only 1 per prompt succeeds, rest error" | A burst of `prompts × outputs` simultaneous requests exceeds the project's per-minute Vertex quota. `generate_one`'s 5 attempts (~40s of backoff) can all land inside the same 60s quota window, so surplus jobs die with 429 while the first wave succeeds. Fix = an **automatic second pass**: a job that fails on a transient error (429/500/503) is resubmitted exactly once after a bounded cool-off that waits out the account cooldowns. `MAX_RETRIES` and the existing backoff are NOT touched. |
| C8 | Built-in video player for the lightbox | Replace the browser-default `<video controls>` in the lightbox with a custom player: seek bar with buffered indicator, play/pause, time, volume + mute (persisted), fullscreen, auto-hiding controls, Space/M/F shortcuts. ←/→ keep navigating between items (unchanged). Full replacement of `static/lightbox.js` is provided in §9. |
| C9 | Drag-to-select works even when the drag starts on a tile (Bananza parity) | `grid.js`'s `onDown` refuses to start the lasso on a tile (`if (tileFrom(e.target)) return;`). Bananza instead records a *potential* drag anywhere, lets the existing movement threshold decide drag-vs-click, and eats the click that ends a lasso. Port exactly that: allow tile starts, `suppressClick` flag, stationary tile press stays a click, plain empty-space click still clears the selection, plus Bananza's edge auto-scroll. |
| C10 | Pressing ✏️ Edit prompt while an item is fullscreen leaves fullscreen first | `.overlay` dialogs are `z-index: 100`; `#lightbox` is `120` — the edit dialog opens UNDER the viewer, invisible. Fix = the `edit` action closes the lightbox before opening the dialog. 🔄 Regenerate and 🗑 Delete deliberately do NOT close it (regen shows the in-viewer spinner; delete advances to the neighbour). |
| F-A | Delete dead code | `gen_engine.test_connection`, `gen_engine.client_from_config`, and the `POST /api/test_connection` route are unused since the multi-account change (the UI uses `/api/accounts/{id}/test`) and build clients without per-account quota-project credentials. Delete all three. |
| F-B | Thread-safety in `retry_failed` | It mutates `_reserved_seqs` without holding `_plan_lock` (every other mutation is locked). Wrap it; also clear the C7 one-shot flag there so a manual Retry earns a fresh automatic second pass. |
| F-C | Bounded MP4 duration read | `_mp4_duration_seconds` reads the entire file into RAM to find one header atom. Read ≤4MB from the head and, if needed, ≤4MB from the tail (moov can sit at either end). |
| F-D | Meta-store cache | `load_meta` re-reads and re-parses `.meta.json` from disk on every call (`list_media` runs on every poll-triggered refresh). Cache per file keyed by `(mtime_ns, size)`, returning a shallow copy; invalidate on write. |

Build order: §2 → §3 → §4 → §5 → §6 → §7 → §8 → §9 → §10 → §11 → §12 → §15 → §16, then verify §14.

---

## §2. NEW FILE — `app/errors.py`  (C2)

Create this file exactly (LF line endings):

```python
"""One place that turns raw Google/SDK error text into a short, plain-English
sentence for the run cards and toasts. The raw text still reaches the terminal
via traceback.print_exc() in gen_engine — this only changes what the USER sees.

Rules:
- Messages that are already written for humans pass through untouched
  (the _PASSTHROUGH prefixes below).
- Otherwise matching is case-insensitive and ORDERED: the first _RULES row
  whose markers hit wins.
- Anything unknown falls back to a trimmed "Google said: ..." so the user
  still gets a hint without a JSON wall.
"""

# Messages this app writes for humans already — never rewrite these.
_PASSTHROUGH = (
    "omni flash",
    "no usable google",
    "every account is rate-limited",
    "saved but couldn't finalise",
    "a reference this item used",
    "that item used",
    "google said:",
)

# (markers tuple, message) — first match wins, top to bottom.
_RULES = [
    (("resource_exhausted", "429", "rate limit", "gave up after retries"),
     "Google's per-minute limit for your project was reached — the app already "
     "waited and retried once by itself. Press 'Retry failed' in a minute to "
     "finish these."),
    (("quota project",),
     "Google's quota-project setting is off — run: gcloud auth "
     "application-default set-quota-project YOUR-PROJECT-ID"),
    (("permission_denied", "service_disabled", "has not been used",
      "is disabled", "403"),
     "Your Google project isn't allowed to use this yet — do Step 2 of the "
     "Guide (enable the API), wait 2 minutes and retry."),
    (("default credentials", "credential", "401", "unauthenticated"),
     "Google sign-in is missing or expired — do Step 4 of the Guide "
     "(gcloud auth application-default login)."),
    (("safety", "no image in response", "no video in response", "blocked"),
     "Google's safety filter blocked this prompt — reword it and try again."),
    (("timed out", "deadline", "took too long"),
     "Google took too long making this one — press 'Retry failed'."),
    (("unavailable", "internal", "500", "503", "overloaded"),
     "Google had a temporary hiccup on their side — press 'Retry failed'."),
    (("not_found", "404", "not found"),
     "This model isn't available on your Google project — pick another model."),
    (("invalid_argument", "invalid argument", "400"),
     "Google rejected the settings for this one (size, ratio or length) — "
     "change them and try again."),
]

_MAX_RAW = 140


def humanize(msg):
    """Short, plain-English version of one failure message."""
    raw = str(msg or "").strip()
    if not raw:
        return "Something went wrong with this one — try again."
    low = raw.lower()
    for p in _PASSTHROUGH:
        if low.startswith(p):
            return raw
    for markers, text in _RULES:
        if any(m in low for m in markers):
            return text
    # Unknown: strip JSON-ish noise, keep it to one trimmed line.
    flat = raw.replace("{", " ").replace("}", " ").replace('"', "'")
    flat = " ".join(flat.split())
    if len(flat) > _MAX_RAW:
        flat = flat[:_MAX_RAW].rstrip() + "…"
    return "Google said: " + flat
```

---

## §3. `app/accounts.py` — one new method  (C7)

Inside `class AccountPool`, insert directly AFTER the `has_alternative` method (anchor: the method that starts with `def has_alternative(self, exclude_id):` — insert after its final `return False` line, before `def acquire(self):`):

```python
    def cooldowns_clear(self):
        """True when no enabled account is sitting in a rate-limit cooldown —
        the §C7 second pass uses this to end its wait early."""
        now = time.time()
        with _state_lock:
            for a in self.accounts:
                h = health_of(a["id"])
                if not h["hard_error"] and h["cooldown_until"] > now:
                    return False
        return True
```

Nothing else in this file changes.

---

## §4. `app/gen_engine.py`  (C2 wiring · C7 · F-A · F-B · F-C)

This file is CRLF — preserve that.

### 4.1 — Import the humanizer (C2)

OLD:
```python
from .config import load_config
```
NEW:
```python
from .config import load_config
from .errors import humanize
```

### 4.2 — Delete the dead client helpers (F-A)

Delete this entire block — from the line `def test_connection(project, location):` down to and including the line `    return make_client(cfg.get("project"), cfg.get("location"))` and the blank lines that follow it, up to (but NOT including) the comment line `# ---------------- images ----------------`. The `# ---------------- client ----------------` header and `make_client` itself STAY.

For clarity, the deleted block is exactly the two functions `test_connection` and `client_from_config` (currently lines ~66–93). After the deletion, `make_client`'s final `raise` is followed by two blank lines and then `# ---------------- images ----------------`.

### 4.3 — Bounded MP4 read (F-C)

Replace the ENTIRE existing function `def _mp4_duration_seconds(path):` (anchor: its current docstring begins `"""Clip length straight from the MP4 header`) with:

```python
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
```

### 4.4 — Second-pass constants + cool-off helper (C7)

Insert directly BEFORE the line `def _run_worker(run_id, pool, kind, model_label, jobs, cfg, project, price):`:

```python
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


```

### 4.5 — The requeue itself (C7)

Inside `_run_worker`, find this exact block:

OLD:
```python
                    try:
                        path, e, cancelled, acct = fut.result()
                    except Exception as exc:
                        path, e, cancelled, acct = None, f"{type(exc).__name__}: {exc}", False, None
                    if path:
```
NEW:
```python
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
```

Notes (already decided — do not deviate):
- The `continue` skips ALL bookkeeping (`done`, pending-chip, error append, `_release_seq`, `_release_name`) — the job is still in flight, so nothing about it may be finalised.
- `accounts_mod.TRANSIENT_MARKERS` already exists and is already imported (`from . import accounts as accounts_mod`).
- The one-shot `_retried` flag guarantees termination: first pass + at most one second pass.

### 4.6 — Humanize the surfaced errors (C2)

Two replacements inside `_run_worker`:

OLD:
```python
                        else:
                            tag = f" [{pool.label_of(acct)}]" if (multi and acct) else ""
                            state["errors"].append(f"{job['n']} v{job['v']}{tag}: {e}")
```
NEW:
```python
                        else:
                            tag = f" [{pool.label_of(acct)}]" if (multi and acct) else ""
                            state["errors"].append(f"{job['n']} v{job['v']}{tag}: {humanize(e)}")
```

OLD:
```python
    except Exception as e:
        traceback.print_exc()
        with RUNS_LOCK:
            state["errors"].append(f"{type(e).__name__}: {str(e)[:300]}")
```
NEW:
```python
    except Exception as e:
        traceback.print_exc()
        with RUNS_LOCK:
            state["errors"].append(humanize(f"{type(e).__name__}: {e}"))
```

The `cancelled` branch (`f"{job['n']} v{job['v']}: cancelled"`) stays exactly as it is.

### 4.7 — Lock + fresh-flag in `retry_failed` (F-B, C7)

OLD:
```python
    # the planned output names were never created — reuse them exactly
    for j in jobs:
        _reserved_seqs.setdefault((project, kind, int(j["n"])), set()).add(round(float(j["seq"]), 6))
```
NEW:
```python
    # the planned output names were never created — reuse them exactly
    with _plan_lock:
        for j in jobs:
            j.pop("_retried", None)   # a manual Retry earns a fresh §C7 second pass
            _reserved_seqs.setdefault((project, kind, int(j["n"])), set()).add(round(float(j["seq"]), 6))
```

---

## §5. `app/projects.py`  (C3 · F-D)

### 5.1 — Cached `load_meta` + invalidating `_write_meta` (F-D)

Insert this line directly AFTER the line `_trashmeta_lock = threading.RLock()`:

```python
_meta_cache = {}      # str(.meta.json path) -> (mtime_ns, size, parsed dict)
```

Replace the ENTIRE existing `def load_meta(name):` function with:

```python
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
```

Replace the ENTIRE existing `def _write_meta(name, m):` function with:

```python
def _write_meta(name, m):
    try:
        p = meta_path(name)
        p.write_text(json.dumps(m, ensure_ascii=False), encoding="utf-8")
        _meta_cache.pop(str(p), None)    # next load re-stats and re-caches
    except Exception:
        notice(f"Couldn't write .meta.json for “{name}” — Regenerate/New won't work "
               "for new items until that's fixed.")
```

(Callers of `_write_meta` already hold `_meta_lock`, so the cache pop is race-free. The shallow copy is sufficient: no caller mutates the inner per-item dicts — `record_meta` assigns a new key, `pop_meta` removes one, `edit_from` copies with `dict(recipe)`.)

### 5.2 — Windows-tolerant unlink (C3)

Insert directly AFTER the comment line `# ---------------- trash ----------------` and BEFORE `def _trash_meta_file():`:

```python
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


```

### 5.3 — `purge_trash`: friendly failure instead of a 500 (C3)

Replace the ENTIRE existing `def purge_trash(name):` function with:

```python
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
```

### 5.4 — `empty_trash`: skip-and-report, keep the recipes of survivors (C3)

Replace the ENTIRE existing `def empty_trash():` function with:

```python
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
```

(`clear_trash_thumbs` wiping ALL trash thumbs is fine even when some media survives — thumbs rebuild on demand.)

---

## §6. `app/server.py`  (C3 · C4 · F-A)

### 6.1 — Delete the dead route (F-A)

Delete this entire block (including its decorator and the blank lines that separate it from its neighbours — leave exactly one blank-line pair between the surrounding routes):

```python
@app.post("/api/test_connection")
async def api_test_connection(request: Request):
    data = await body(request)
    ok, message = gen_engine.test_connection(data.get("project"), data.get("location"))
    return {"ok": ok, "message": message}
```

### 6.2 — Cache prefix for trash media (C4)

OLD:
```python
CACHE_PREFIXES = ("/media/", "/thumb/", "/preview/", "/trash-thumb/")
```
NEW:
```python
CACHE_PREFIXES = ("/media/", "/thumb/", "/preview/", "/trash-thumb/", "/trash-media/")
```

### 6.3 — `empty` returns the locked list (C3)

Replace the ENTIRE existing `api_trash_empty` route with:

```python
@app.post("/api/trash/empty")
def api_trash_empty():
    purged, locked = projects.empty_trash()
    gen_engine.bump()
    return {"ok": True, "purged": purged, "locked": locked}
```

### 6.4 — Full trash media with Range support (C4)

Insert directly AFTER the existing `api_trash_thumb` route (anchor: the function that returns `_send(thumbs.ensure_trash_thumb(Path(name).name), request)`), separated by two blank lines:

```python
@app.get("/trash-media/{name}")
def api_trash_media(name: str, request: Request):
    """Full-quality media straight from the trash, for the §C4 viewer."""
    return _send(projects.trash_path(Path(name).name), request)
```

(`Path` is already imported in this file; `projects.trash_path` already refuses anything outside the trash and non-media extensions, and `_send` 404s on None.)

---

## §7. `static/index.html`  (C4 · C8 · doc text)

### 7.1 — Replace the lightbox stage with the custom player shell (C8)

OLD (one line):
```html
  <div class="lb-stage"><img class="lb-img" alt=""><video class="lb-vid hidden" controls preload="auto"></video></div>
```
NEW:
```html
  <div class="lb-stage">
    <img class="lb-img" alt="">
    <div class="lb-player hidden">
      <video class="lb-vid" preload="auto" playsinline></video>
      <div class="vp-center hidden">▶</div>
      <div class="vp-bar">
        <div class="vp-seek"><div class="vp-buf"></div><div class="vp-fill"></div><div class="vp-knob"></div></div>
        <div class="vp-row">
          <button class="vp-btn vp-play" title="Play / pause (Space)">▶</button>
          <span class="vp-time mono">0:00 / 0:00</span>
          <div class="vp-flex"></div>
          <button class="vp-btn vp-mute" title="Mute (M)">🔊</button>
          <input class="vp-vol" type="range" min="0" max="1" step="0.05" title="Volume">
          <button class="vp-btn vp-fs" title="Fullscreen (F)">⛶</button>
        </div>
      </div>
    </div>
  </div>
```
(Note: the `controls` attribute and the `hidden` class are intentionally gone from the `<video>` — the wrapper `.lb-player` carries visibility now.)

### 7.2 — Trash viewer overlay (C4)

The `#lightbox` block ends with:
```html
  <div class="lb-spinner hidden"><i></i><span>regenerating…</span></div>
</div>
```
Insert AFTER that closing `</div>` (and before the `<script src="/static/grid.js"></script>` line), separated by one blank line on each side:

```html
<div id="trash-view" class="hidden">
  <div class="tv-actions">
    <button class="icon-btn" data-tv="restore" title="Restore to the open project">↩</button>
    <button class="icon-btn danger" data-tv="purge" title="Delete forever">🗑</button>
    <button class="icon-btn" data-tv="close" title="Close">✕</button>
  </div>
  <button class="tv-nav prev" data-tv="prev" title="Previous">‹</button>
  <div class="tv-stage">
    <img class="tv-img" alt="">
    <video class="tv-vid hidden" controls preload="metadata"></video>
  </div>
  <button class="tv-nav next" data-tv="next" title="Next">›</button>
  <div class="tv-caption"><span class="tv-name mono"></span><span class="tv-proj"></span></div>
</div>
```

### 7.3 — Trash hint text (C4)

OLD:
```html
      <p class="hint">Deleted items land here. Tap one, then Restore (goes back into the open
        project) or Delete forever.</p>
```
NEW:
```html
      <p class="hint">Deleted items land here. Tap to select · double-click to view it
        fullscreen. Restore puts it back into the open project.</p>
```

### 7.4 — Cheatsheet addition (C8)

Find this exact `<li>`:
```html
        <li><b>Click</b> a tile → fullscreen viewer: <b>←/→</b> browse, <b>Del</b> deletes, <b>Esc</b> closes,
          wheel zooms, double-click toggles 2.5×</li>
```
Insert directly AFTER it:
```html
        <li><b>Videos in the viewer:</b> Space play/pause · M mute · F fullscreen · drag the bar
          to seek — <b>←/→</b> still switch between items</li>
```

---

## §8. `static/style.css`  (C1 · C4 · C8)

### 8.1 — Stacking order for tiles (C1)

Five edits. Each `OLD` excerpt is unique in the file.

OLD:
```css
.tile video.tp { position: absolute; inset: 0; }
```
NEW:
```css
.tile video.tp { position: absolute; inset: 0; z-index: 1; }
```

OLD:
```css
.tile-badge {
  position: absolute;
  left: 6px; top: 6px;
```
NEW:
```css
.tile-badge {
  position: absolute;
  left: 6px; top: 6px;
  z-index: 2;
```

OLD:
```css
.tile-dur {
  position: absolute;
  right: 6px; bottom: 6px;
```
NEW:
```css
.tile-dur {
  position: absolute;
  right: 6px; bottom: 6px;
  z-index: 2;
```

OLD:
```css
.tile-play {
  position: absolute;
  inset: 0;
```
NEW:
```css
.tile-play {
  position: absolute;
  inset: 0;
  z-index: 2;
```

OLD:
```css
.tile-acts {
  position: absolute;
  right: 5px; top: 5px;
```
NEW:
```css
.tile-acts {
  position: absolute;
  right: 5px; top: 5px;
  z-index: 3;
```

And one more for completeness (the regenerate overlay must beat everything):

OLD:
```css
.tile-busy {
  position: absolute;
  inset: 0;
```
NEW:
```css
.tile-busy {
  position: absolute;
  inset: 0;
  z-index: 4;
```

### 8.2 — Append to the END of the file (C8 player + C4 trash viewer)

Append this entire block at the end of `style.css`:

```css

/* ---------- built-in video player (v1.1 §C8) ---------- */
.lb-player { position: relative; display: flex; align-items: center; justify-content: center; }
.lb-player.hide-cursor { cursor: none; }
.lb-player video { max-width: 96vw; max-height: 88vh; display: block; background: #000; border-radius: 10px; }
.lb-player:fullscreen { width: 100vw; height: 100vh; background: #000; }
.lb-player:fullscreen video { max-width: 100vw; max-height: 100vh; width: 100%; height: 100%; object-fit: contain; border-radius: 0; }
.vp-center {
  position: absolute; inset: 0; margin: auto;
  width: 74px; height: 74px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 1.7rem; color: #fff; cursor: pointer;
  background: rgba(10, 10, 14, .55);
  border: 1px solid rgba(255, 255, 255, .18);
  text-shadow: 0 2px 12px rgba(0, 0, 0, .8);
}
.vp-bar {
  position: absolute; left: 0; right: 0; bottom: 0;
  padding: 26px 14px 10px;
  background: linear-gradient(transparent, rgba(5, 5, 8, .85));
  opacity: 1; transition: opacity .18s ease;
  border-radius: 0 0 10px 10px;
}
.vp-bar.idle { opacity: 0; pointer-events: none; }
.vp-seek { position: relative; height: 14px; cursor: pointer; touch-action: none; }
.vp-seek::before {
  content: ''; position: absolute; left: 0; right: 0; top: 5px; height: 4px;
  border-radius: 4px; background: rgba(255, 255, 255, .18);
}
.vp-buf, .vp-fill { position: absolute; left: 0; top: 5px; height: 4px; border-radius: 4px; width: 0; pointer-events: none; }
.vp-buf { background: rgba(255, 255, 255, .3); }
.vp-fill { background: var(--accent-hi); }
.vp-knob {
  position: absolute; top: 7px; left: 0; width: 12px; height: 12px; margin-left: -6px;
  border-radius: 50%; background: #fff; box-shadow: 0 1px 6px rgba(0, 0, 0, .6);
  transform: translateY(-3px); pointer-events: none;
}
.vp-row { display: flex; align-items: center; gap: 10px; margin-top: 6px; }
.vp-flex { flex: 1; }
.vp-btn {
  font: inherit; font-size: .95rem; line-height: 1;
  padding: 6px 9px; border-radius: 9px; cursor: pointer;
  border: 1px solid rgba(255, 255, 255, .14);
  background: rgba(16, 16, 22, .7); color: #eee;
}
.vp-btn:hover { background: rgba(var(--accent), .45); }
.vp-time { font-size: .74rem; color: rgba(255, 255, 255, .85); }
.vp-vol { width: 90px; accent-color: var(--accent-hi); }
#lightbox.vid .lb-caption { bottom: 68px; }

/* ---------- fullscreen trash viewer (v1.1 §C4) ---------- */
#trash-view {
  position: fixed; inset: 0;
  background: rgba(6, 6, 9, .96);
  z-index: 130;                      /* above the settings overlay (100) */
  display: flex; align-items: center; justify-content: center;
  animation: lb-in .14s ease;
}
#trash-view .tv-stage { width: 100%; height: 100%; display: flex; align-items: center; justify-content: center; overflow: hidden; }
#trash-view .tv-img, #trash-view .tv-vid { max-width: 96vw; max-height: 86vh; display: block; }
#trash-view .tv-actions { position: absolute; right: 16px; top: 16px; display: flex; gap: 6px; z-index: 3; }
#trash-view .tv-actions .icon-btn { background: rgba(20, 20, 26, .9); font-size: 1rem; padding: 7px 10px; }
#trash-view .tv-caption {
  position: absolute; left: 50%; bottom: 16px; transform: translateX(-50%);
  max-width: 76vw; display: flex; gap: 10px; align-items: baseline;
  font-size: .8rem; background: rgba(16, 16, 22, .85);
  border: 1px solid var(--chrome); border-radius: 10px; padding: 6px 12px;
}
#trash-view .tv-name { color: var(--dim); white-space: nowrap; }
#trash-view .tv-proj { color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tv-nav {
  position: absolute; top: 50%; transform: translateY(-50%);
  font: inherit; font-size: 1.7rem; line-height: 1;
  width: 44px; height: 64px; border-radius: 12px;
  border: 1px solid var(--chrome); background: rgba(20, 20, 26, .75);
  color: var(--text); cursor: pointer; z-index: 3;
}
.tv-nav:hover { background: rgba(var(--accent), .35); }
.tv-nav.prev { left: 14px; }
.tv-nav.next { right: 14px; }
```

---

## §9. `static/lightbox.js` — FULL FILE REPLACEMENT  (C8)

Replace the entire file with the content below (LF line endings). The public API is unchanged (`LB.open/close/refresh/isOpen/current/setBusy/onAction/init`), so `app.js` needs no changes for this.

Decisions baked in (do not deviate):
- ←/→ still navigate between items (never seek). Seeking is drag-the-bar only.
- Space = play/pause, M = mute, F = fullscreen — only when the current item is a video, never while typing, never while another overlay is open.
- Volume and mute persist in `localStorage` (`megagen_vol`, `megagen_mute`).
- The control bar auto-hides after 2.2s of mouse idle while playing; it always shows while paused or seeking.
- `pUnload()` removes the `src` and calls `vid.load()` — that actually releases the HTTP stream / file handle (relevant to §C3 on Windows).
- Esc inside browser-fullscreen exits fullscreen; Esc otherwise closes the lightbox.

```javascript
/* Fullscreen viewer: full-quality media, wheel/double-click zoom for images,
   ←/→ navigation, and a built-in video player (v1.1 §C8): custom seek bar with
   buffered indicator, play/pause, time, volume + mute (persisted), fullscreen,
   auto-hiding controls, Space/M/F shortcuts. Zoom math ported from Bananza's
   v4.9 lightbox. */
(function () {
  'use strict';

  const LB = {
    name: null,
    onAction: function () {},
    isOpen: function () { return !!LB.name; },
    current: function () { return LB.name; },
  };
  window.Lightbox = LB;

  let root, img, badge, nameEl, promptEl, spinner;
  let player, vid, center, bar, seek, buf, fill, knob, btnPlay, timeEl, btnMute, vol, btnFs;
  const z = { z: 1, tx: 0, ty: 0 };
  const P = { idleTimer: 0, seeking: false };
  let pendingNext = null;

  function isVid(item) { return item && item.kind === 'video'; }

  /* ---------------- zoom (images only — unchanged from v1.0) ---------------- */
  function zApply() {
    img.style.transformOrigin = 'center center';
    img.style.transform = z.z > 1
      ? 'translate(' + z.tx + 'px,' + z.ty + 'px) scale(' + z.z + ')' : '';
    img.style.cursor = z.z > 1 ? 'grab' : '';
  }
  function zClamp() {
    const r = img.getBoundingClientRect();
    const mx = Math.max(0, (r.width - window.innerWidth) / 2) + 60;
    const my = Math.max(0, (r.height - window.innerHeight) / 2) + 60;
    z.tx = Math.max(-mx, Math.min(mx, z.tx));
    z.ty = Math.max(-my, Math.min(my, z.ty));
  }
  function zReset() { z.z = 1; z.tx = 0; z.ty = 0; zApply(); }
  function zAt(cx, cy, f) {
    const nz = Math.max(1, Math.min(12, z.z * f));
    f = nz / z.z;
    if (f === 1) return;
    const r = img.getBoundingClientRect();
    z.tx += (cx - (r.left + r.width / 2)) * (1 - f);
    z.ty += (cy - (r.top + r.height / 2)) * (1 - f);
    z.z = nz;
    if (z.z <= 1.001) { z.z = 1; z.tx = 0; z.ty = 0; }
    zClamp();
    zApply();
  }

  /* ---------------- built-in video player (§C8) ---------------- */
  function fmtT(s) {
    if (!isFinite(s) || s < 0) s = 0;
    s = Math.floor(s);
    return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
  }
  function playGlyph() {
    btnPlay.textContent = vid.paused ? '▶' : '⏸';
    center.classList.toggle('hidden', !vid.paused && !vid.ended);
  }
  function muteGlyph() { btnMute.textContent = (vid.muted || vid.volume === 0) ? '🔇' : '🔊'; }
  function timePaint() {
    timeEl.textContent = fmtT(vid.currentTime) + ' / ' + fmtT(vid.duration);
    const d = vid.duration || 0;
    const pct = d ? (vid.currentTime / d * 100) + '%' : '0%';
    fill.style.width = pct;
    knob.style.left = pct;
  }
  function bufPaint() {
    const d = vid.duration || 0;
    let end = 0;
    for (let i = 0; i < vid.buffered.length; i++) end = Math.max(end, vid.buffered.end(i));
    buf.style.width = d ? Math.min(100, end / d * 100) + '%' : '0%';
  }
  function barShow() {
    bar.classList.remove('idle');
    player.classList.remove('hide-cursor');
    clearTimeout(P.idleTimer);
    if (!vid.paused && !P.seeking) {
      P.idleTimer = setTimeout(function () {
        bar.classList.add('idle');
        player.classList.add('hide-cursor');
      }, 2200);
    }
  }
  function toggle() {
    if (!vid.getAttribute('src')) return;
    if (vid.paused) { const p = vid.play(); if (p && p.catch) p.catch(function () {}); }
    else vid.pause();
  }
  function setVol(v) {
    vid.volume = Math.max(0, Math.min(1, v));
    muteGlyph();
    try { localStorage.setItem('megagen_vol', String(vid.volume)); } catch (e) { /* private mode */ }
  }
  function setMuted(m) {
    vid.muted = m;
    muteGlyph();
    try { localStorage.setItem('megagen_mute', m ? '1' : '0'); } catch (e) { /* private mode */ }
  }
  function fsToggle() {
    if (document.fullscreenElement) {
      const p = document.exitFullscreen(); if (p && p.catch) p.catch(function () {});
    } else if (player.requestFullscreen) {
      const p = player.requestFullscreen(); if (p && p.catch) p.catch(function () {});
    }
  }
  function seekTo(clientX) {
    const r = seek.getBoundingClientRect();
    const f = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
    if (vid.duration) vid.currentTime = f * vid.duration;
    timePaint();
  }
  function pLoad(src) {
    player.classList.remove('hidden');
    center.textContent = '▶';
    center.removeAttribute('title');
    if (vid.getAttribute('src') !== src) {
      vid.src = src;
      const p = vid.play();
      if (p && p.catch) p.catch(function () {});
    }
    playGlyph(); muteGlyph(); timePaint(); bufPaint(); barShow();
  }
  function pUnload() {
    clearTimeout(P.idleTimer);
    P.seeking = false;
    player.classList.add('hidden');
    vid.pause();
    vid.removeAttribute('src');
    vid.load();                       // actually drop the stream / file handle
  }

  /* ---------------- painting ---------------- */
  function paint() {
    const item = Grid.byName(LB.name);
    if (!item) return;
    const src = Grid.mediaUrl('full', item);
    root.classList.toggle('vid', !!isVid(item));
    if (isVid(item)) {
      img.classList.add('hidden');
      img.removeAttribute('src');
      pLoad(src);
    } else {
      pUnload();
      img.classList.remove('hidden');
      if (img.getAttribute('src') !== src) img.src = src;
    }
    badge.textContent = '#' + item.n;
    nameEl.textContent = item.name;
    const prompt = (item.meta && item.meta.prompt) || '';
    promptEl.textContent = prompt.length > 160 ? prompt.slice(0, 160) + '…' : prompt;
    promptEl.title = prompt;
  }

  LB.open = function (name) {
    const item = Grid.byName(name);
    if (!item || item.ghost) return;
    const changed = LB.name !== name;
    LB.name = name;
    if (changed) zReset();
    root.classList.remove('hidden');
    paint();
    LB.setBusy(Grid.busy.has(name));
  };

  LB.close = function () {
    if (document.fullscreenElement) {
      const p = document.exitFullscreen(); if (p && p.catch) p.catch(function () {});
    }
    LB.name = null;
    pendingNext = null;
    root.classList.add('hidden');
    root.classList.remove('vid');
    pUnload();
    img.removeAttribute('src');
    zReset();
  };

  LB.setBusy = function (on) { spinner.classList.toggle('hidden', !on); };

  LB.refresh = function () {
    if (!LB.name) return;
    if (!Grid.byName(LB.name)) {
      const next = pendingNext && Grid.byName(pendingNext) ? pendingNext : null;
      pendingNext = null;
      if (next) { LB.open(next); return; }
      LB.close();
      return;
    }
    paint();
    LB.setBusy(Grid.busy.has(LB.name));
  };

  function step(delta) {
    const i = Grid.indexOf(LB.name);
    if (i < 0) return;
    let j = i + delta;
    while (Grid.items[j] && Grid.items[j].ghost) j += delta;
    const item = Grid.items[j];
    if (item) LB.open(item.name);
  }

  function neighbourOf(name) {
    const i = Grid.indexOf(name);
    for (let j = i + 1; j < Grid.items.length; j++) if (!Grid.items[j].ghost) return Grid.items[j].name;
    for (let j = i - 1; j >= 0; j--) if (!Grid.items[j].ghost) return Grid.items[j].name;
    return null;
  }

  function act(a) {
    if (a === 'close') { LB.close(); return; }
    if (a === 'delete') pendingNext = neighbourOf(LB.name);
    LB.onAction(a, LB.name);
  }

  function onKey(e) {
    if (!LB.name) return;
    if (document.querySelector('.overlay:not(.hidden)')) return;
    const t = document.activeElement;
    const typing = t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable);
    if (typing) return;
    const item = Grid.byName(LB.name);
    if (e.key === 'Escape') {
      e.preventDefault();
      if (document.fullscreenElement) {
        const p = document.exitFullscreen(); if (p && p.catch) p.catch(function () {});
        return;
      }
      LB.close();
    } else if (e.key === 'ArrowRight') { e.preventDefault(); step(1); }
    else if (e.key === 'ArrowLeft') { e.preventDefault(); step(-1); }
    else if (e.key === 'Delete') { e.preventDefault(); act('delete'); }
    else if ((e.key === ' ' || e.code === 'Space') && isVid(item)) { e.preventDefault(); toggle(); }
    else if ((e.key === 'm' || e.key === 'M') && isVid(item)) { e.preventDefault(); setMuted(!vid.muted); }
    else if ((e.key === 'f' || e.key === 'F') && isVid(item)) { e.preventDefault(); fsToggle(); }
  }

  LB.init = function () {
    root = document.getElementById('lightbox');
    img = root.querySelector('.lb-img');
    player = root.querySelector('.lb-player');
    vid = root.querySelector('.lb-vid');
    center = root.querySelector('.vp-center');
    bar = root.querySelector('.vp-bar');
    seek = root.querySelector('.vp-seek');
    buf = root.querySelector('.vp-buf');
    fill = root.querySelector('.vp-fill');
    knob = root.querySelector('.vp-knob');
    btnPlay = root.querySelector('.vp-play');
    timeEl = root.querySelector('.vp-time');
    btnMute = root.querySelector('.vp-mute');
    vol = root.querySelector('.vp-vol');
    btnFs = root.querySelector('.vp-fs');
    badge = root.querySelector('.lb-badge');
    nameEl = root.querySelector('.lb-name');
    promptEl = root.querySelector('.lb-prompt');
    spinner = root.querySelector('.lb-spinner');

    root.querySelectorAll('.lb-actions [data-lb]').forEach((b) => {
      b.addEventListener('click', (e) => { e.stopPropagation(); act(b.dataset.lb); });
    });
    root.addEventListener('click', (e) => {
      if (e.target === root || e.target.classList.contains('lb-stage')) LB.close();
    });

    root.addEventListener('wheel', (e) => {
      const item = Grid.byName(LB.name);
      if (isVid(item)) return;
      if (e.target.closest && e.target.closest('.lb-actions, .lb-caption')) return;
      e.preventDefault();
      zAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.25 : 0.8);
    }, { passive: false });

    img.addEventListener('dblclick', (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (z.z > 1) zReset(); else zAt(e.clientX, e.clientY, 2.5);
    });

    let drag = null;
    img.addEventListener('mousedown', (e) => {
      if (z.z <= 1 || e.button !== 0) return;
      e.preventDefault();
      drag = { x: e.clientX, y: e.clientY };
      img.style.cursor = 'grabbing';
    });
    window.addEventListener('mousemove', (e) => {
      if (!drag) return;
      z.tx += e.clientX - drag.x;
      z.ty += e.clientY - drag.y;
      drag = { x: e.clientX, y: e.clientY };
      zClamp();
      zApply();
    });
    window.addEventListener('mouseup', () => { if (drag) { drag = null; zApply(); } });

    /* ---- player wiring (§C8) ---- */
    vid.addEventListener('play', function () { playGlyph(); barShow(); });
    vid.addEventListener('pause', function () { playGlyph(); barShow(); });
    vid.addEventListener('ended', function () { playGlyph(); barShow(); });
    vid.addEventListener('timeupdate', timePaint);
    vid.addEventListener('durationchange', function () { timePaint(); bufPaint(); });
    vid.addEventListener('progress', bufPaint);
    vid.addEventListener('volumechange', function () {
      muteGlyph();
      vol.value = vid.muted ? 0 : vid.volume;
    });
    vid.addEventListener('error', function () {
      if (!vid.getAttribute('src')) return;   // unload() fires a spurious error
      center.textContent = '⚠';
      center.title = "This clip can't be played";
      center.classList.remove('hidden');
    });
    vid.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
    vid.addEventListener('dblclick', function (e) { e.preventDefault(); e.stopPropagation(); fsToggle(); });
    center.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
    btnPlay.addEventListener('click', function (e) { e.stopPropagation(); toggle(); });
    btnMute.addEventListener('click', function (e) { e.stopPropagation(); setMuted(!vid.muted); });
    btnFs.addEventListener('click', function (e) { e.stopPropagation(); fsToggle(); });
    vol.addEventListener('input', function () { setMuted(false); setVol(Number(vol.value)); });
    bar.addEventListener('click', function (e) { e.stopPropagation(); });
    player.addEventListener('mousemove', barShow);
    seek.addEventListener('pointerdown', function (e) {
      e.preventDefault(); e.stopPropagation();
      P.seeking = true;
      try { seek.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      seekTo(e.clientX);
    });
    seek.addEventListener('pointermove', function (e) { if (P.seeking) seekTo(e.clientX); });
    seek.addEventListener('pointerup', function (e) {
      if (!P.seeking) return;
      P.seeking = false;
      try { seek.releasePointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      seekTo(e.clientX);
      barShow();
    });

    // restore saved volume / mute once at startup
    let v0 = 1, m0 = false;
    try {
      const rawV = localStorage.getItem('megagen_vol');
      v0 = rawV === null ? 1 : Number(rawV);
      if (!isFinite(v0)) v0 = 1;
      m0 = localStorage.getItem('megagen_mute') === '1';
    } catch (e) { /* private mode */ }
    vid.volume = Math.max(0, Math.min(1, v0));
    vid.muted = m0;
    vol.value = vid.muted ? 0 : vid.volume;

    window.addEventListener('keydown', onKey, true);
  };
})();
```

---

## §10. `static/app.js`  (C5 · C6)

This file is CRLF — preserve that.

### 10.1 — Keyboard on the confirm dialog (C5)

Anchor — this exact line inside `wireStatic()`:
```javascript
    $('confirm-ok').onclick = () => { const cb = confirmCb; confirmCb = null; closeModal('confirm-modal'); if (cb) cb(); };
```
Insert directly AFTER it:
```javascript
    // §C5: keyboard on the confirm dialog — Del confirms, Esc cancels.
    // e.repeat is ignored so HOLDING Del (which opened the dialog) can never
    // blow straight through it; only a fresh second press confirms.
    document.addEventListener('keydown', (e) => {
      if ($('confirm-modal').classList.contains('hidden')) return;
      if (e.repeat) return;
      if (e.key === 'Delete') {
        e.preventDefault(); e.stopPropagation();
        $('confirm-ok').click();
      } else if (e.key === 'Escape') {
        e.preventDefault(); e.stopPropagation();
        $('confirm-cancel').click();
      }
    }, true);
```

### 10.2 — Clear the stale callback when an overlay is dismissed generically

OLD (inside `wireStatic()`):
```javascript
      ov.addEventListener('click', (e) => {
        if (e.target === ov || e.target.closest('.close')) {
          ov.classList.add('hidden');
          if (ov.id === 'settings-modal' && window.Settings) Settings.stopHealthPoll();
        }
      });
```
NEW:
```javascript
      ov.addEventListener('click', (e) => {
        if (e.target === ov || e.target.closest('.close')) {
          ov.classList.add('hidden');
          if (ov.id === 'settings-modal' && window.Settings) Settings.stopHealthPoll();
          if (ov.id === 'confirm-modal') confirmCb = null;   // §C5: no stale confirm
        }
      });
```

### 10.3 — Rewrite `copyItem` (C6)

Replace the ENTIRE existing `async function copyItem(name) { ... }` function (it currently spans from `  async function copyItem(name) {` down to the `  }` immediately before `  async function deleteNames(names) {`) with:

```javascript
  async function copyItem(name) {
    const item = Grid.byName(name);
    if (!item) return;
    if (item.kind === 'video') {
      const text = (item.meta && item.meta.prompt) || '';
      if (!text) { toast('This clip has no saved prompt text.', 'err'); return; }
      try { await navigator.clipboard.writeText(text); toast('Prompt copied'); }
      catch (e) { toast('The browser blocked the copy — click the page once, then try again.', 'err'); }
      return;
    }
    if (!navigator.clipboard || !navigator.clipboard.write || !window.ClipboardItem) {
      toast('Copying images needs Chrome or Edge.', 'err');
      return;
    }
    // §C6: generated images are always PNG on disk — hand the browser the
    // original bytes directly. The old canvas re-encode handed clipboard.write
    // a PROMISE for the data; Chrome resolves the write before that promise
    // settles, so a slow or failed encode showed "Image copied" while the
    // clipboard stayed empty. Materialise the Blob FIRST, then write.
    try {
      const r = await fetch(Grid.mediaUrl('full', item));
      if (!r.ok) throw new Error('HTTP ' + r.status);
      let blob = await r.blob();
      if (blob.type !== 'image/png') blob = new Blob([blob], { type: 'image/png' });
      await navigator.clipboard.write([new window.ClipboardItem({ 'image/png': blob })]);
      toast('Image copied');
    } catch (e) {
      if (e && e.name === 'NotAllowedError') {
        toast('The browser blocked the copy — click anywhere on the page once, then try again.', 'err');
      } else {
        toast("Couldn't copy this image: " + (e && e.message ? e.message : e), 'err');
      }
    }
  }
```

Nothing else in `app.js` changes. (The Ctrl+C grid shortcut and both tile/lightbox copy buttons all route through this one function.)

---

## §11. `static/settings.js`  (C3 toast · C4 viewer)

### 11.1 — State declarations

OLD:
```javascript
  let U = null;                 // {$, esc, jpost, api, toast, oops, confirmBox, refresh}
  let trashSel = null;
```
NEW:
```javascript
  let U = null;                 // {$, esc, jpost, api, toast, oops, confirmBox, refresh}
  let trashSel = null;
  let trashItems = [];          // §C4: the list the trash gallery shows, in order
  const TV = { idx: -1 };       // §C4: fullscreen trash viewer state
```

### 11.2 — Remember the item list

Inside `Settings.loadTrash`, OLD:
```javascript
      const items = await U.api('/api/trash');
      trashSel = null;
```
NEW:
```javascript
      const items = await U.api('/api/trash');
      trashItems = items;
      trashSel = null;
```

### 11.3 — The viewer module (C4)

Insert this block directly AFTER the closing `};` of `Settings.loadTrash` and BEFORE the line `  let accts = [];`:

```javascript
  // ---------- fullscreen trash viewer (v1.1 §C4) ----------
  function tvEls() {
    const root = document.getElementById('trash-view');
    return { root: root,
             img: root.querySelector('.tv-img'), vid: root.querySelector('.tv-vid'),
             name: root.querySelector('.tv-name'), proj: root.querySelector('.tv-proj') };
  }

  function tvPaint() {
    const t = trashItems[TV.idx];
    if (!t) { tvClose(); return; }
    const e = tvEls();
    const src = '/trash-media/' + encodeURIComponent(t.name);
    if (t.kind === 'video') {
      e.img.classList.add('hidden');
      e.img.removeAttribute('src');
      e.vid.classList.remove('hidden');
      if (e.vid.getAttribute('src') !== src) {
        e.vid.src = src;
        const p = e.vid.play();
        if (p && p.catch) p.catch(function () {});
      }
    } else {
      e.vid.classList.add('hidden');
      e.vid.pause();
      e.vid.removeAttribute('src');
      e.vid.load();
      e.img.classList.remove('hidden');
      if (e.img.getAttribute('src') !== src) e.img.src = src;
    }
    e.name.textContent = t.orig;
    e.proj.textContent = t.project ? 'from “' + t.project + '”' : '';
  }

  function tvOpen(name) {
    const i = trashItems.findIndex(function (t) { return t.name === name; });
    if (i < 0) return;
    TV.idx = i;
    tvEls().root.classList.remove('hidden');
    tvPaint();
  }

  function tvClose() {
    const e = tvEls();
    TV.idx = -1;
    e.root.classList.add('hidden');
    e.vid.pause();
    e.vid.removeAttribute('src');
    e.vid.load();                 // §C3: actually release the file handle
    e.img.removeAttribute('src');
  }

  function tvStep(d) {
    if (TV.idx < 0) return;
    const j = TV.idx + d;
    if (j < 0 || j >= trashItems.length) return;
    TV.idx = j;
    tvPaint();
  }
```

### 11.4 — Wiring (C4): gallery double-click, viewer buttons, keys

Insert this block inside `Settings.init`, directly BEFORE the final closing `  };` of `Settings.init` (i.e. after the whole `$('btn-trash-empty-yes').onclick = ...` handler):

```javascript
    // ---------- §C4: trash viewer wiring ----------
    $('trash-gallery').addEventListener('dblclick', (e) => {
      const ti = e.target.closest('.ti');
      if (!ti) return;
      trashSel = ti.dataset.name;
      $('trash-gallery').querySelectorAll('.ti').forEach((x) => x.classList.toggle('on', x === ti));
      tvOpen(ti.dataset.name);
    });

    const tvRoot = document.getElementById('trash-view');
    tvRoot.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-tv]');
      if (b) {
        const t = trashItems[TV.idx];
        if (b.dataset.tv === 'close') { tvClose(); return; }
        if (b.dataset.tv === 'prev') { tvStep(-1); return; }
        if (b.dataset.tv === 'next') { tvStep(1); return; }
        if (!t) return;
        tvClose();                              // §C3: release the handle FIRST
        try {
          if (b.dataset.tv === 'restore') {
            const r = await jpost('/api/trash/restore', { name: t.name, project: MG.project });
            toast('Restored ' + r.name + ' to “' + MG.project + '”.');
          } else if (b.dataset.tv === 'purge') {
            await jpost('/api/trash/purge', { name: t.name });
            toast('Deleted forever.');
          }
          await Settings.loadTrash();
          await U.refresh();
        } catch (err) { oops(err); }
        return;
      }
      if (e.target === tvRoot || e.target.classList.contains('tv-stage')) tvClose();
    });

    // capture phase so Esc closes the VIEWER, never the settings modal under it
    document.addEventListener('keydown', (e) => {
      if (TV.idx < 0) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); tvClose(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); tvStep(1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); tvStep(-1); }
    }, true);
```

### 11.5 — Existing trash buttons release the viewer first (C3)

Three one-line insertions — in each handler, `tvClose();` becomes the FIRST statement of the function body:

OLD:
```javascript
    $('btn-trash-restore').onclick = async () => {
      if (!trashSel) { toast('Tap an item in the trash first.', 'err'); return; }
```
NEW:
```javascript
    $('btn-trash-restore').onclick = async () => {
      tvClose();
      if (!trashSel) { toast('Tap an item in the trash first.', 'err'); return; }
```

OLD:
```javascript
    $('btn-trash-purge').onclick = async () => {
      if (!trashSel) { toast('Tap an item in the trash first.', 'err'); return; }
```
NEW:
```javascript
    $('btn-trash-purge').onclick = async () => {
      tvClose();
      if (!trashSel) { toast('Tap an item in the trash first.', 'err'); return; }
```

### 11.6 — Empty-trash handler: close viewer + report locked files (C3)

Replace the ENTIRE existing `$('btn-trash-empty-yes').onclick = async () => { ... };` handler with:

```javascript
    $('btn-trash-empty-yes').onclick = async () => {
      tvClose();
      $('trash-confirm').classList.add('hidden');
      try {
        const r = await jpost('/api/trash/empty', {});
        let msg = r.purged ? 'Trash emptied — ' + r.purged + ' item(s) gone for good.'
          : 'Trash was already empty.';
        if (r.locked && r.locked.length) {
          msg = 'Emptied ' + r.purged + ' item(s). ' + r.locked.length
            + " couldn't be deleted because Windows still has them open"
            + ' (a thumbnail may be building) — try again in a moment.';
        }
        toast(msg, r.locked && r.locked.length ? 'err' : null);
        await Settings.loadTrash();
        await U.refresh();
      } catch (e) { oops(e); }
    };
```

---

## §12. NEW FILE — `tests/test_fixes.py`

Create this file exactly (LF line endings). It follows the house test style (print PASS/FAIL, exit code), spends no credits, and never touches the user's real `config.json`, `usage.json`, or `output/.trash`. PIL tracebacks printed by the thumbnail builder during the §C7 runs are expected noise (same as `test_video_duration.py`).

```python
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
```

---

## §15. `static/grid.js`  (C9 — drag-select starts anywhere, like Bananza)

Four edits + one added listener. This file is LF. The existing 5px movement threshold in `onMove` already separates a click from a drag — it stays exactly as is.

### 15.1 — Allow the lasso to start on a tile

OLD:
```javascript
  // lasso — starts only on empty grid space, never on a tile
  let lasso = null;

  function onDown(e) {
    if (e.button !== 0) return;
    if (tileFrom(e.target)) return;
    const rect = scrollEl.getBoundingClientRect();
    lasso = {
      x0: e.clientX - rect.left + scrollEl.scrollLeft,
      y0: e.clientY - rect.top + scrollEl.scrollTop,
      base: (e.ctrlKey || e.metaKey || e.shiftKey) ? new Set(Grid.selection) : new Set(),
      moved: false,
    };
    e.preventDefault();
  }
```
NEW:
```javascript
  // lasso — §C9: starts anywhere in the grid, tiles included (Bananza parity).
  // A press that never moves past the 5px threshold is still a plain click.
  let lasso = null;
  let suppressClick = false;   // §C9: eats the click that ends a drag-lasso

  function onDown(e) {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest('.tile-acts')) return;   // buttons need clean clicks
    const rect = scrollEl.getBoundingClientRect();
    lasso = {
      x0: e.clientX - rect.left + scrollEl.scrollLeft,
      y0: e.clientY - rect.top + scrollEl.scrollTop,
      base: (e.ctrlKey || e.metaKey || e.shiftKey) ? new Set(Grid.selection) : new Set(),
      moved: false,
      fromTile: !!tileFrom(e.target),   // a stationary press on a tile is a click, not a lasso
    };
    e.preventDefault();                 // also stops the native image ghost-drag
  }
```

### 15.2 — Edge auto-scroll while lassoing (Bananza parity)

OLD (the first lines of `onMove`):
```javascript
  function onMove(e) {
    if (!lasso) return;
    const rect = scrollEl.getBoundingClientRect();
    const x1 = e.clientX - rect.left + scrollEl.scrollLeft;
```
NEW:
```javascript
  function onMove(e) {
    if (!lasso) return;
    const rect = scrollEl.getBoundingClientRect();
    if (lasso.moved) {
      // §C9: auto-scroll when lassoing near the top/bottom edge — same 34px
      // band and 16px step Bananza uses. Only once the drag is real, so a
      // stationary press near an edge never scrolls. Mutate scrollTop BEFORE
      // computing x1/y1 so the rectangle uses the post-scroll coordinates.
      if (e.clientY > rect.bottom - 34) scrollEl.scrollTop += 16;
      else if (e.clientY < rect.top + 34 && scrollEl.scrollTop > 0) scrollEl.scrollTop -= 16;
    }
    const x1 = e.clientX - rect.left + scrollEl.scrollLeft;
```

### 15.3 — `onUp`: suppress the trailing click; empty-space click still clears

OLD:
```javascript
  function onUp() {
    if (!lasso) return;
    const moved = lasso.moved;
    lasso = null;
    lassoEl.classList.add('hidden');
    if (!moved) {
      Grid.clearSelection();
    }
  }
```
NEW:
```javascript
  function onUp() {
    if (!lasso) return;
    const moved = lasso.moved, fromTile = lasso.fromTile;
    lasso = null;
    lassoEl.classList.add('hidden');
    if (moved) {
      // §C9: the mouseup over a tile fires a click next — eat it so ending a
      // lasso can never open the fullscreen viewer (Bananza's 150ms trick).
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 150);
    } else if (!fromTile) {
      Grid.clearSelection();     // plain click on empty space still clears
    }
  }
```

### 15.4 — `onClick`: honour the suppression

OLD:
```javascript
  function onClick(e) {
    const tile = tileFrom(e.target);
```
NEW:
```javascript
  function onClick(e) {
    if (suppressClick) { suppressClick = false; return; }   // §C9: this click just ended a lasso
    const tile = tileFrom(e.target);
```

### 15.5 — No native image drag, ever

OLD (inside `Grid.init`):
```javascript
    gridEl.addEventListener('click', onClick);
```
NEW:
```javascript
    gridEl.addEventListener('click', onClick);
    gridEl.addEventListener('dragstart', (e) => e.preventDefault());   // §C9: no image ghost-drag
```

Behaviour matrix this must produce (verify mentally before moving on):
- press tile + drag → lasso selects everything it touches, INCLUDING the starting tile; release opens nothing.
- press tile, no move, release → opens/selects exactly as v1.0 (click, Ctrl-click toggle, Shift-click range).
- Ctrl/Shift + drag from a tile → ADDS to the existing selection (`base` logic, unchanged).
- press empty space, no move → selection clears (unchanged).
- press a tile action button (🔄✨✏️📋🗑) → never starts a lasso.

---

## §16. `static/app.js` — one more edit  (C10)

This file is CRLF — preserve that.

OLD (inside `tileAction`):
```javascript
    if (act === 'edit') { openEditModal(name); return; }
```
NEW:
```javascript
    if (act === 'edit') {
      // §C10: overlays sit at z100, the fullscreen viewer at z120 — the edit
      // dialog would open UNDERNEATH it. Leave the viewer first. Regenerate
      // and Delete intentionally keep the viewer open (in-viewer spinner /
      // advance-to-neighbour behaviour).
      if (Lightbox.isOpen()) Lightbox.close();
      openEditModal(name);
      return;
    }
```

Do NOT restack z-indexes and do NOT generalise this to the other actions — the two exceptions above are deliberate.

---

## §13. REGRESSION CONTRACT — do not touch

- `bulk_banana_gui.py`, `run_megagen.py`, `requirements.txt`, port 7861, the launcher banner.
- `make_client`, `gen_image`, `gen_video_veo`, `gen_video_omni`, `_camel`, `_video_bytes_from_op`, `generate_one`, `generate_one_video` — including `MAX_RETRIES` and the existing backoff/`has_alternative` logic. §C7 lives entirely in `_run_worker`.
- The seq/name reservation semantics (`reserve_seqs`, `reserve_seq_after`, `claim_name`), media sorting `(n, seq, ts, name)`, the restore flow, the preserved-inputs flow (`input_path` / `preserve_input`).
- `AccountPool.acquire/release/has_alternative/_rank/capacity`, the health model, `notice()`.
- The grid virtualisation, layout, tile painting, and hover-preview logic in `grid.js` — §C9 touches ONLY the lasso block (`onDown`, `onMove`'s first lines, `onUp`, `onClick`'s first line, one `dragstart` listener); `schedule`, `posOf`, `paint`, `startPreview` and everything else are untouched. C1 remains CSS-only. Also unchanged: the polling/`bump()` model, `_send()` Range serving, the pricing tables, `plan`/validation logic.
- `tests/test_accounts.py` and `tests/test_video_duration.py` are NOT modified and must still pass unchanged (they stub below/above the layers touched here — this was verified when this spec was written).

---

## §14. DEFINITION OF DONE — verify every line, in order

Automated (no Google project needed):
1. `python -m py_compile run_megagen.py app/*.py tests/*.py` → silent success.
2. `node --check static/app.js static/grid.js static/lightbox.js static/settings.js` → all OK.
3. `python tests/test_accounts.py` → `20 passed, 0 failed`.
4. `python tests/test_video_duration.py` → `16 passed, 0 failed`.
5. `python tests/test_fixes.py` → all pass, exit 0.
6. `grep -rn "test_connection\|client_from_config" app/ static/` → zero matches (F-A fully removed).
7. `grep -n "humanize" app/gen_engine.py` → the import, the two append sites (§4.6), and the mention inside the §4.5 comment — four lines, no more.
8. `grep -n "trash-media" app/server.py static/settings.js` → route, cache prefix, and viewer URL present.

Manual, app running locally (`python run_megagen.py`), using already-generated local media:
9. **C1** — hover a video tile until the preview plays: the 🔄✨✏️📋🗑 buttons, #badge, duration chip and ▶ remain visible ON TOP of the moving preview, and remain clickable.
10. **C4** — Settings → double-click a trash item: fullscreen viewer opens ABOVE the settings modal; ←/→ and the on-screen arrows step through items; a trashed video plays with default controls; ↩ restores into the open project; 🗑 purges; Esc closes the viewer only (settings stays open); clicking the empty stage closes it.
11. **C3** — with a trashed video open in the viewer, press Esc then immediately Empty trash: no error. If Windows ever still holds a file, the toast reads "…couldn't be deleted because Windows still has them open — try again in a moment" and a second Empty succeeds.
12. **C5** — select 2+ items, press Del → confirm dialog appears; press Del again → deletes. Re-test holding Del down: the dialog must NOT auto-confirm. Esc cancels. Clicking outside the dialog then pressing Del later must not fire a stale delete.
13. **C6** — hover an image tile → 📋 → paste into Paint/Photoshop: the full-resolution image pastes. Also verify Ctrl+C with one selected tile, and 📋 inside the lightbox.
14. **C8** — open a video in the lightbox: custom bar (no native controls); seek by dragging; buffered strip visible; Space/M/F work; volume survives a page reload; controls fade after ~2s of playback and reappear on mouse move; double-click video = fullscreen; ←/→ still switch items; Esc in fullscreen exits fullscreen, second Esc closes; the caption sits above the control bar, not under it.
15. **C9** — press directly on a tile and drag: the lasso appears after ~5px and selects everything it touches including the starting tile; releasing the drag does NOT open the fullscreen viewer. A plain click on a tile still opens it; Ctrl-click still toggles; Ctrl+drag starting on a tile adds to the selection; a plain click on empty space still clears; dragging near the bottom edge auto-scrolls; an image can never be ghost-dragged.
16. **C10** — open an item fullscreen, press ✏️: the viewer closes and the Edit-prompt dialog is immediately visible with the textarea focused. From a grid tile, ✏️ behaves as before. 🔄 in the viewer still shows the in-viewer "regenerating…" spinner; 🗑 still advances to the neighbour.
17. **C2/C7** 🔌 (needs a configured Google project) — run a burst (e.g. 10 prompts × 4 outputs on one account): rate-limited jobs visibly finish late instead of erroring (run card keeps counting), and any error that does surface reads as one plain sentence ("Google's per-minute limit …"), never raw JSON.

— end of spec —
