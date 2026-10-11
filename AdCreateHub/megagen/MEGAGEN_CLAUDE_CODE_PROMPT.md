# MEGAGEN — Complete Build Specification for Claude Code

You are building **MegaGen**, a desktop-first bulk media generator. Every product and design decision has already been made and is written in this document. Your job is implementation only.

---

## 0. RULES FOR YOU (CLAUDE CODE) — READ FIRST

1. **Do not make product decisions.** Layout, naming, behaviors, schemas, colors, endpoints — everything is specified below. Implement exactly what is written.
2. **Do not add features, options, or "improvements"** that are not in this document. No extra settings, no alternative modes, no refactors of the spec.
3. **`bulk_banana_gui.py` is in this folder as a reference implementation.** Section 3 lists exactly which constants and functions to port from it. Port their *logic* faithfully (adapted from Gradio to plain Python/FastAPI) — these are battle-tested against the real Vertex API. Do not "modernize" their error handling or retry behavior.
4. **When to ask the user a question:** only if (a) this spec contradicts itself, (b) a library/API behaves differently than the spec assumes and you cannot resolve it after 2 genuine attempts, or (c) something in the spec is impossible on the user's machine. Otherwise do not ask — the answer is in this document.
5. **Follow the build phases in Section 11 in order.** Each phase ends with a verification step; run it before moving on.
6. **Target platform: Windows desktop first** (the reference app is Windows-centric), but keep macOS/Linux working via the same `platform.system()` branching the reference uses. Desktop browsers only (Chrome/Edge). **No mobile optimization of any kind** — do not add responsive breakpoints, touch handlers, or viewport hacks.
7. Keep each source file under ~900 lines. Comment only where behavior is non-obvious.
8. Python 3.10+. No Node, no build step, no database, no Docker. Everything installs with `pip`.

---

## 1. PRODUCT SUMMARY

MegaGen is the "mega bulk" successor to Bananza (`bulk_banana_gui.py`). Bananza generates media from **one prompt at a time**. MegaGen generates media from an **entire numbered list of prompts in one press of Generate**.

Same foundation as Bananza:
- Google Vertex AI via the `google-genai` SDK, `vertexai=True`, Project ID + Location, auth via `gcloud auth application-default login` (the $300 free-trial flow). No API keys.
- Nano Banana image models, Veo/Omni video models — same model tables and pricing.
- Projects (output folders), spend tracker against $300, recoverable Trash, background generation with live progress.

Two workspaces, shown as two tabs inside every project:

**IMAGES tab** — the user imports a numbered list of text-to-image prompts (paste or .txt file). They optionally bulk-add reference images whose filenames bind them to prompts: `7-1.png` = prompt 7's reference 1, `7-2.png` = prompt 7's reference 2 (`7.png` alone = reference 1). Inside a prompt's text, `@1` `@2` refer to *that prompt's* reference 1 and 2 (identical tag semantics to Bananza). One Generate press runs every prompt **in ascending number order**, with a global "Outputs per prompt" of 1–10. Output files are named `{promptNumber}-gav{6 random digits}.{ext}`.

**VIDEOS tab** — the user bulk-drops start-frame images named by prompt number (`12.png` = start frame for prompt 12) and imports a numbered prompt list the same way. Each matched (prompt, frame) pair generates image-to-video clips; global "Outputs per prompt" of 1–3. Output files are named `{promptNumber}Vid-gav{6 random digits}.mp4`. Prompts without a frame are **skipped** (never text-to-video) and clearly flagged.

Carried over from Bananza, per the owner's explicit list: drag-to-select (plus Ctrl/Shift-click, Ctrl+A, Esc, Del), Projects, credit/spend usage, Trash, Delete, Copy (images copy the image to clipboard; **for videos Copy copies the prompt text** to clipboard), and:
- **Regenerate** (replaces Bananza's "Reuse prompt"): re-runs the exact same generation; on success the old file moves to Trash and the new file takes its **exact place** in the grid (same filename, same sort position).
- **New** (new feature): re-runs the exact same generation and places the result **immediately next to** the original, keeping the original.

Performance is a core requirement: the app must stay smooth with hundreds of items. Video tiles in the grid never load full-quality video — they show generated poster thumbnails, with an optional muted low-res hover preview. Full quality plays only in the fullscreen lightbox. Media is served with HTTP Range support and long-lived caching to minimize buffering.

---

## 2. TECH STACK & PROJECT STRUCTURE

Why not Gradio like the reference app: Bananza spends ~1,700 lines of JavaScript fighting Gradio's gallery to get selection, lightbox, and hover actions. MegaGen's virtualized grid, poster thumbnails, and Range-served video need direct control. Decision (final): **FastAPI backend + one vanilla HTML/JS/CSS frontend. No frameworks, no build step.**

`requirements.txt` (exact):
```
google-genai
fastapi
uvicorn[standard]
pillow
imageio-ffmpeg
```
`imageio-ffmpeg` ships a static ffmpeg binary via pip — it is the ONLY way ffmpeg enters this project. Never require a system ffmpeg. Get the binary path with `imageio_ffmpeg.get_ffmpeg_exe()`.

Repository layout (create exactly this):
```
megagen/
  run_megagen.py        # entry point: console banner, uvicorn launch, opens browser
  app/
    __init__.py
    server.py           # FastAPI app, routes, static mounting, cache middleware
    config.py           # config.json load/save, defaults, constants
    models_tables.py    # model tables, pricing, durations — ported from reference
    gen_engine.py       # client, gen_image, gen_video_veo, gen_video_omni, retry, workers, RUNS
    projects.py         # project folders, media listing, meta store, trash
    prompts.py          # prompt-list parser, ref/frame filename matcher, validation
    thumbs.py           # image thumbs, video posters, low-res previews (ffmpeg)
    usage.py            # usage.json spend tracker
  static/
    index.html
    app.js              # may be split into app.js + grid.js + lightbox.js if >900 lines
    style.css
  output/               # created at runtime (projects live here)
  config.json           # created at runtime
  usage.json            # created at runtime
  requirements.txt
  README.md             # 15 lines max: install, run, where the setup guide lives
```

The server binds `127.0.0.1:7861` (7861 so it never collides with Bananza's 7860). `run_megagen.py --no-browser` skips auto-opening the browser. There is no `--share` flag in MegaGen.

---

## 3. EXACT PORTS FROM `bulk_banana_gui.py`

Port the following into the files named. "Port" = copy the logic and constants, adjust names/imports, remove Gradio-specific code (`gr.Error`, `gr.Info`, `gr.update`) — errors become raised `GenError(msg)` exceptions or returned error strings per the contracts in this spec.

Into `models_tables.py`:
- `PRICE_PER_IMAGE` (1K $0.067, 2K $0.101, 4K $0.151)
- `MODELS` (Nano Banana 2 / 2 Lite / Pro → gemini-3.1-flash-image / gemini-3.1-flash-lite-image / gemini-3-pro-image)
- `VEO3_DURS`, `VEO2_DURS`, `OMNI_DURS`, `VIDEO_MODELS` (all seven entries with id/kind/pps/audio/clip/durs), `VIDEO_RESOLUTIONS`, `VIDEO_ASPECTS`, `VIDEO_POLL_SECS = 10`, `VIDEO_TIMEOUT_SECS = 20*60`
- `ASPECT_CHOICES` (same 11 options incl. "auto"), `IMAGE_EXTS`, `VIDEO_EXTS`, `MEDIA_EXTS`
- `model_durs()`, `snap_dur()`
- `MAX_RETRIES = 5`, `TAG_RE = re.compile(r"@(?:image\s*)?(\d+)", re.I)`

Into `gen_engine.py` (functions renamed only if noted):
- `make_client(project, location)` — including the credential-hint error message
- `test_connection(project, location)` — including every hint branch (credentials / quota project / 403 / 404). Return `(ok: bool, message: str)` instead of markdown.
- `gen_image(...)` — including the `image_size`-rejection fallback retry and the "No image in response (safety filter?)" error
- `generate_one(...)` — the retry loop: retryable on `429/500/503/RESOURCE_EXHAUSTED/UNAVAILABLE`, backoff `min(2**attempt + random.uniform(0,2), 60)`, returns `(path|None, error|None)`
- `_mp4_duration_seconds(path)` — the mvhd atom parser, unchanged, sanity clamp included
- `_camel`, `_video_bytes_from_op`
- `gen_video_veo(...)` — LRO submit/poll, the defensive kwargs-dropping loop, Veo 2 quirks (no audio, no resolution, min 5s). **Delete the `last_frame` branch entirely** — MegaGen has no end frames.
- `gen_video_omni(...)` — including the too-old-library message and the duration-fallback retry
- `generate_one_video(...)` — including the Omni-not-enabled 404 message

Into `projects.py`:
- `sanitize_name()`, the trash naming scheme from `_trash_file` (`HHMMSS_name.ext`), `_trash_orig_name()` (both regex branches), restore-collision naming (`name (2).png`), `open_output_folder()` platform branching
- The `.meta.json` load/save pattern with a lock (`_load_meta`, `record_meta` equivalents)

Into `usage.py`: `load_usage`, `save_usage`, `add_usage` with its lock, session dict.

Into `server.py`: the media `Cache-Control: public, max-age=604800` middleware concept from `_media_cache_app_kwargs` (applied to `/media/`, `/thumb/`, `/preview/` responses).

Into `run_megagen.py`: the entire console experience — `_Colors`, `_enable_ansi`, `_gradient_text`, `_box_line`, `_print_banner`, `_Loader`, `_print_ready`, console title, the friendly crash handler with "Press Enter to close". Rebrand every string from "Bananza"/banana to "MegaGen" and the emoji from 🍌 to ⚡. Banner tagline: "MEGAGEN — mega bulk generator". Keep the immediate "MEGAGEN is waking up…" print before heavy imports.

Into `static/index.html`: the `SETUP_GUIDE` text (lines ~3578–3687 of the reference) as the content of the setup-guide modal, with "Bananza" replaced by "MegaGen". Keep the gcloud steps verbatim — they are correct.

---

## 4. ON-DISK DATA MODEL

```
output/
  .trash/                          # flat; trashed files + .trashmeta.json
  <ProjectName>/
    prompts-images.json            # image-tab prompt list (schema below)
    prompts-videos.json            # video-tab prompt list
    refs/                          # reference images, normalized names: 7-1.png, 7-2.jpg
    frames/                        # video start frames, normalized names: 12.png
    images/                        # generated images: 7-gav482913.png
    videos/                        # generated videos: 7Vid-gav482913.mp4
    .thumbs/                       # cache: <mediafile>.thumb.webp, <videofile>.poster.jpg,
                                   #        <videofile>.preview.mp4 (safe to delete anytime)
    .meta.json                     # per-generated-file metadata, keyed by basename
```

A "project" is any directory in `output/` except `.trash`. On startup create `output/` and a project named `Default` if no projects exist. Project names run through `sanitize_name()` (ported). Deleting a project moves its whole folder into `.trash/` with the `name_YYYYMMDD_HHMMSS` scheme (ported). The `Default` project cannot be deleted (same rule as Bananza's Unsorted).

### 4.1 prompts-images.json / prompts-videos.json
```json
{
  "prompts": [
    {"n": 1, "text": "A neon fox in the rain"},
    {"n": 2, "text": "Same fox, @1 as the style reference"}
  ],
  "updated": 1753500000
}
```
`n` is the user's number from the imported file — an integer label, unique within the list, gaps allowed, always displayed and processed in ascending order. Prompt text edits from the UI rewrite this file immediately.

### 4.2 .meta.json (one per project, guarded by a threading.Lock)
Keyed by generated basename. Every value is the full **recipe** needed to replay the generation:
```json
{
  "7-gav482913.png": {
    "kind": "image",
    "n": 7,
    "seq": 2.0,
    "prompt": "raw prompt text as it was at generation time",
    "refs": ["7-1.png", "7-2.png"],
    "model": "Nano Banana 2",
    "aspect": "16:9",
    "size": "2K",
    "ts": 1753500123
  },
  "7Vid-gav118266.mp4": {
    "kind": "video",
    "n": 7,
    "seq": 1.0,
    "prompt": "…",
    "frame": "7.png",
    "model": "Veo 3.1 Fast · video",
    "aspect": "16:9",
    "res": "720p",
    "dur": 8,
    "audio": true,
    "ts": 1753500456
  }
}
```

### 4.3 `seq` — the grid-position rule (this powers Regenerate and New)
The grid is sorted by `(n ascending, seq ascending)`. `seq` is a float assigned at creation:
- A normal run producing K outputs for prompt n assigns seq = existing max seq for that n (or 0) + 1, +2, … in job order.
- **New** on a file with seq S inserts at `S + 0.001 * k` where k is the smallest positive integer making the value unused for that n → the copy lands *immediately after* its original.
- **Regenerate** keeps the original's exact `seq` AND exact filename → same place, guaranteed.
Never renumber existing seq values.
Seq values are assigned at job-build time under a per-project lock that also holds an in-memory set of *reserved* seqs per (project, n) for jobs still in flight — "existing max" means max over `.meta.json` entries AND reservations, so two stacked runs for the same prompt can never hand out the same seq. Reservations clear when their job finishes or its run ends. Final sort everywhere is `(n asc, seq asc, ts asc, name asc)` — the ts/name tie-break makes ordering deterministic even if a seq duplicate ever slips through.

### 4.4 .trash/.trashmeta.json
```json
{"143012_7-gav482913.png": {"orig": "7-gav482913.png", "project": "MyProj", "meta": { …the .meta.json entry… }}}
```
Written on every file-trash; used on restore to re-insert the meta entry into the destination project (restore goes into the currently open project, images→images/, videos→videos/, same as Bananza).

### 4.5 config.json
```json
{
  "project": "", "location": "global",
  "last_project": "Default", "last_tab": "images",
  "workers": 3,
  "img": {"model": "Nano Banana 2", "aspect": "auto", "size": "1K", "outputs": 1},
  "vid": {"model": "Veo 3.1 Fast · video", "aspect": "16:9", "res": "720p", "dur": 8, "audio": true, "outputs": 1},
  "grid_cols": 6,
  "hover_previews": true
}
```
Every UI control writes its value to config the moment it changes (Bananza's remember-my-settings behavior). Missing keys fall back to these defaults.

---

## 5. PROMPT PARSER (`prompts.py`) — EXACT SPEC

Input: raw text (from a pasted textarea or an uploaded `.txt` file, UTF-8, BOM tolerated). Output: `(prompts: list[{n,text}], warnings: list[str], errors: list[str])`. Errors block saving; warnings do not.

Rules, in order:
1. Normalize line endings to `\n`.
2. A line **starts a new prompt** iff it matches `^\s*(\d{1,4})\s*[\.\)\:]\s*(.*)$` or `^\s*(\d{1,4})\s+-\s+(.*)$` (i.e. `1. text`, `1) text`, `1: text`, `1 - text`). The number is group 1; the remainder of the line is the first line of the prompt text.
3. Any other non-matching line is appended (with `\n`) to the current prompt's text. Non-matching lines before the first numbered line: ignored, one warning "Ignored N line(s) before the first numbered prompt."
4. If **no** line matches the pattern at all: fall back to blank-line splitting — each non-empty block becomes a prompt, auto-numbered 1..N in order, warning "No numbering found — prompts were auto-numbered 1–N."
5. Trim each prompt's text. A prompt whose text is empty after trimming → warning "Prompt {n} is empty — it was skipped." and it is dropped.
6. Duplicate numbers → **error** "Duplicate prompt number {n} (lines X and Y)." List every duplicate. Errors mean nothing is saved.
7. More than 500 prompts → error "That's {count} prompts — the limit is 500 per list."
8. Sort ascending by n before returning.

Importing a list into a tab **replaces** that tab's existing list. If the existing list is non-empty, the frontend shows a confirm dialog first ("Replace the current {count} prompts? Generated media is not affected."). After saving, individual prompt texts remain editable inline in the panel; edits persist immediately and affect only future generations.

## 5.1 Reference-image filename matcher (Images tab)

Accepted upload extensions: `.png .jpg .jpeg .webp`. For each uploaded file, match the *stem* against `^(\d{1,4})(?:[-_ ](\d{1,2}))?$`.
- `7` → prompt 7, ref slot 1. `7-2` / `7_2` / `7 2` → prompt 7, ref slot 2.
- Non-matching filenames are rejected; collect them and toast one message: "Skipped {k} file(s) with unrecognized names: a.png, b.png… (expected 7.png or 7-2.png)".
- Ref slot must be 1–6 (Gemini multi-image sanity cap). Slot >6 → rejected with the same toast mechanism ("ref number above 6").
- The file is copied into `refs/` as `{n}-{slot}{ext}` (lowercased ext, `7.png` normalized to `7-1.png`). If that name exists, the new upload **replaces** it (old one is NOT trashed — refs are user inputs, not generated media) and the toast says "Replaced reference {n}-{slot}".
- A ref may target a prompt number not (yet) in the list — accept it, but the UI flags it (Section 8.3).
- Deleting a ref in the UI deletes the file from `refs/`.

## 5.2 Start-frame filename matcher (Videos tab)

Same accepted extensions. Stem must match `^(\d{1,4})$` exactly — `12.png` yes, `12-1.png` no (reject with the unrecognized-names toast; expected form in the message: "12.png"). Copied to `frames/{n}{ext}`; same-number re-upload replaces the old file even across extensions (uploading `12.jpg` deletes `frames/12.png` first — one frame per prompt, always). Frames for numbers not in the prompt list are accepted and flagged.

## 5.3 @tag semantics inside a prompt (identical to Bananza, scoped per prompt)

`TAG_RE` finds tags. For prompt n with refs `[r1..rk]` (slot order):
- Every `@j` must satisfy `1 ≤ j ≤ k`, else pre-flight validation error (Section 6.4).
- At job build time: rewrite each `@j` → `image j`, dedupe tag order, and if the prompt has refs prepend `"You are given {k} reference image(s), in order: image 1, image 2…. "` exactly like the reference app's `plan_jobs` does.
- **All k refs are always attached** to the request in slot order whenever a prompt has refs — with or without @tags in the text. (No tags simply means the text doesn't name them explicitly.)
- Prompts with zero refs are plain text-to-image.

---

## 6. GENERATION ENGINE (`gen_engine.py`)

### 6.1 Run registry (ported concept)
Module-level `RUNS = {}` + `RUNS_LOCK`, `DATA_VERSION` int + `bump()`, run ids from an incrementing counter. Run state dict:
```python
{"id": 3, "kind": "image"|"video", "project": "MyProj", "mode": "Images · 24 prompts × 2",
 "total": 48, "done": 0, "ok": 0, "errors": [], "failed_jobs": [],  # failed_jobs: list of job dicts for retry
 "started": time.time(), "finished": False, "cancel": False}
```
Keep at most the 4 most recent finished runs (ported cleanup rule).

### 6.2 Job building — Images
`POST /api/generate` with kind=image: load the project's image prompt list, refs map, and settings `{model, aspect, size, outputs}` (outputs clamped 1–10).
1. Run pre-flight validation (6.4). Any error → HTTP 400 with the error list; nothing starts.
2. For each prompt in ascending n, for variant v in 1..outputs, build one job:
   `{"n": n, "prompt_final": rewritten_text, "refs": [abs paths in slot order], "out": images/  n-gav######.ext, "seq": assigned per §4.3, "meta": recipe}`
   `######` = 6 random digits (`random.randint(0, 999999):06d`), re-rolled while the filename exists in images/, .trash/, or .meta.json. Extension: `.png` (gen_image writes the bytes it gets; Nano Banana returns PNG).
   `image_config` passed to gen_image = `{"image_size": size}`, plus `"aspect_ratio": aspect` **only when aspect != "auto"** (ported from the reference's `start_generation` — "auto" means omit the field, never send the string "auto").
3. Jobs are submitted to a `ThreadPoolExecutor(max_workers=workers)` **in list order** (ascending n, then variant) — the pool naturally starts the first `workers` jobs first, so generation proceeds in prompt order with completions allowed to arrive out of order.
4. Worker loop = ported `_worker` pattern: as each future completes, update done/ok/errors, `add_usage(1, PRICE_PER_IMAGE[size])`, write the recipe into `.meta.json`, `bump()`. On failure append `"{n} v{v}: {error}"` to errors AND the original job dict to `failed_jobs`. Honor `cancel`: before executing each job body, if the run's cancel flag is set, return a skipped marker (counts toward done, not ok, message "cancelled" — do not add to failed_jobs).
5. Mode string: `"Images · {P} prompts × {outputs}"`.

### 6.3 Job building — Videos
Same flow with kind=video and settings `{model, aspect(16:9|9:16), res, dur (snap_dur'd), audio, outputs}` (outputs clamped 1–3).
- Only prompts that have a matching frame produce jobs (prompt n × outputs). Skipped prompts are reported in the start response (`"skipped": [n, …]`) — they are NOT errors.
- Output name `n`Vid-gav`######`.mp4 (unique the same way). vcfg built exactly like the reference (`aspect/res/dur/audio`, Veo 2 quirks live inside the ported gen function). The single frame is the `image` argument for Veo, `image_paths=[frame]` for Omni.
- Worker cap: `max_workers = min(workers, 2)` (ported rule — video LROs are quota-limited).
- Per-file cost on completion: `pps * (_mp4_duration_seconds(file) or dur)` (ported).
- Mode string: `"Videos · {P} prompts × {outputs}"`.

### 6.4 Pre-flight validation (shared, returns list of error strings)
Images: (a) prompt list empty → "Import prompts first."; (b) for each prompt, every @tag within 1..ref-count → "Prompt {n} uses @{j} but has only {k} reference(s)."; (c) outputs in range. Videos: (a) list empty; (b) at least one prompt has a frame → otherwise "No prompt has a start frame — add frames named like 12.png."; (c) outputs in range; (d) aspect is 16:9 or 9:16 ("auto" arriving from config is coerced to 16:9 silently, ported rule). Client connection errors surface as the ported `make_client` messages.

### 6.5 Regenerate (single item)
`POST /api/media/regenerate {project, name}`:
1. Read the file's recipe from `.meta.json` (404 error toast if missing: "No saved recipe for this item.").
2. Mark the tile busy (the run this creates carries `"replaces": name` so the frontend can overlay a spinner on that exact tile).
3. Generate to a temp name in the same folder using the recipe (same model/settings/prompt/refs-or-frame; missing ref/frame files → error "A reference this item used was deleted.").
4. **Only on success:** move the original to Trash (with trashmeta), rename temp → the ORIGINAL filename, write the recipe (fresh ts, same n and seq) to `.meta.json`. On failure the original is untouched.
5. This is a 1-job run in RUNS (mode `"Regenerate {name}"`), so progress/errors surface like everything else.

### 6.6 New (single item)
`POST /api/media/new {project, name}`: same replay, but the output gets a fresh `…-gav######` name, `seq = original seq + 0.001k` (§4.3), and the original is untouched. 1-job run, mode `"New from {name}"`.

### 6.7 Retry failed
`POST /api/runs/{id}/retry_failed`: builds a new run from `failed_jobs`, reusing the exact same planned output names (they were never created). Original run's failed_jobs are cleared. **Only bulk runs (§6.2/§6.3) offer retry** — Regenerate/New runs never populate `failed_jobs` and their cards show no Retry button; the user simply clicks Regenerate/New again on the tile.

### 6.8 Cancel
`POST /api/runs/{id}/cancel` sets `cancel=True`. In-flight API calls finish naturally; queued jobs skip. The run card shows "stopping…" until finished.

---

## 7. THUMBNAILS, POSTERS, PREVIEWS (`thumbs.py`)

All cache files live in `<project>/.thumbs/` and are regenerated on demand if missing (safe to delete the folder).
**Staleness rule (critical):** before serving any cached thumb/poster/preview, compare its mtime to the source file's mtime; if the source is newer, delete the cache file and rebuild. Regenerate reuses filenames, so without this rule a regenerated item would keep showing its old thumbnail forever.
- **Image thumb:** `{basename}.thumb.webp` — Pillow, longest side 480px, WebP quality 78. Built lazily on first `GET /thumb/...` request (then cached), and eagerly right after each image generation completes (in the worker thread, best-effort try/except).
- **Video poster:** `{basename}.poster.jpg` — ffmpeg (imageio-ffmpeg binary): `-ss 0 -i in.mp4 -frames:v 1 -vf scale=480:-2 -q:v 4 out.jpg`. Built eagerly after each video completes; lazily on demand otherwise.
- **Video hover preview:** `{basename}.preview.mp4` — `-i in.mp4 -vf scale=360:-2 -an -c:v libx264 -preset veryfast -crf 30 -movflags +faststart out.mp4`. Built in a single dedicated background thread with a queue (never blocks generation workers), only when `hover_previews` is true in config. If ffmpeg fails, log and move on — the grid falls back to the poster.
- All ffmpeg/Pillow work is subprocess/thread-side; endpoints serving a not-yet-built artifact build it synchronously on first request (images/posters are fast) except previews, which return 404 until ready (frontend just keeps showing the poster).

---

## 8. HTTP API (all JSON unless noted; errors: HTTP 400 `{"error": "msg"}` — the frontend toasts `error`)

State & polling
- `GET /api/boot` → `{config, projects: [...], version, tables}` (called once on page load). `tables` contains the model lists, per-model durations/audio/kind flags, `PRICE_PER_IMAGE`, per-second video prices, and `ASPECT_CHOICES` — the frontend computes cost estimates and builds dropdowns from this, so prices/models are never duplicated in JS.
- `GET /api/state?project=P&tab=images|videos` → `{prompts: [...], refs: {n: [slots]}, frames: {n: "12.png"}, media: [{name, n, seq, kind, ts, has_preview, meta}...sorted (n,seq)], runs: [...], usage: {...}, trash_count}`
- `GET /api/poll?since=V` → `{version}` — returns immediately; the frontend polls every 2s and refetches `/api/state` only when version changed (Bananza's DATA_VERSION pattern). Every mutation on the server calls `bump()`.

Projects
- `POST /api/projects {name}` · `DELETE /api/projects/{name}` (→ trash; Default protected) · `POST /api/projects/{name}/open_folder`

Prompts / refs / frames (all bump)
- `POST /api/prompts {project, tab, text}` → parser result `{prompts, warnings, errors}`; saves only if no errors
- `PATCH /api/prompts {project, tab, n, text}` (inline edit) · `DELETE /api/prompts {project, tab, n}` (removes one prompt; its media stays)
- `POST /api/refs {project}` multipart files → `{added, replaced, skipped: [...]}` · `DELETE /api/refs {project, name}`
- `POST /api/frames {project}` multipart → same shape · `DELETE /api/frames {project, name}`

Generation
- `POST /api/generate {project, kind, settings}` → `{run_id, total, skipped}` or 400 with `{"errors": [...]}`
- `POST /api/media/regenerate` · `POST /api/media/new` · `POST /api/runs/{id}/retry_failed` · `POST /api/runs/{id}/cancel`
- `POST /api/media/delete {project, names: [...]}` → trash each (with trashmeta), bump. No server-side confirm — the frontend confirms for 2+.

Trash
- `GET /api/trash` → `[{name, orig, kind, ts}]` · `POST /api/trash/restore {name, project}` · `POST /api/trash/purge {name}` · `POST /api/trash/empty`
- `GET /trash-thumb/{name}` — thumb/poster for a trashed file, cached in `output/.trash/.thumbs/` with the same staleness rule as §7 (the settings-modal trash gallery uses only this endpoint, never full media)

Settings
- `POST /api/config {partial}` (deep-merge into config.json) · `POST /api/test_connection {project, location}` → `{ok, message}`

Media (GET, cache middleware applies)
- `GET /media/{project}/{name}` — full file, **must support Range** (use `starlette` FileResponse behind an endpoint that validates `name` via the ported basename-only lookup; Range comes free with Starlette ≥0.30 FileResponse + `request.headers` passthrough — verify with a curl Range request in Phase 5 and, if Starlette's version doesn't honor Range, implement a manual 206 handler)
- `GET /thumb/{project}/{name}` · `GET /preview/{project}/{name}` — always append `?v={file mtime}` on the frontend for cache-busting (critical: Regenerate reuses filenames)

Security floor: every filename parameter goes through `Path(name).name` and is looked up only inside the project's known folders (ported `find_media` philosophy). Server binds localhost only.

---

## 9. FRONTEND SPEC (`index.html`, `app.js`, `style.css`)

### 9.1 Visual design system (same family as Bananza — Flow-style dark)
- Canvas background: `radial-gradient(1200px 620px at 50% -240px, rgba(139,92,246,.10), transparent 72%), #0e0e11`. Accent: violet `rgb(139,92,246)`; hover/focus lavender `#a78bfa`. Neutral chrome borders `rgba(255,255,255,.07)`; violet is reserved for interaction (focus, hover, selection, the Generate button).
- Fonts: Google Fonts import — `Outfit` (700/800) for the logo/headings, `Inter` for everything else, `JetBrains Mono` for numbers/costs. Base font-size 14px.
- Logo top-left: `⚡ MegaGen` — Outfit 800, 1.45rem, the exact animated gradient text treatment from Bananza's `h1.app-title` (violet→pink sheen, 9s loop).
- Radii: cards 12–14px, buttons 10–11px. Thin custom scrollbars (violet thumb, ported CSS). Button hover `brightness(1.12)`, active `scale(.985)`.
- Toasts: fixed top-right stack, dark card with violet border; info auto-dismiss 6s, errors 10s with a ✕.
- Confirm dialogs: centered overlay card (Bananza's confirm-card look): message + red confirm + Cancel.

### 9.2 Layout (single page, desktop only, min sensible window 1280px)
```
┌ TOPBAR: ⚡ MegaGen | project ▾ (+ new) | [ Images | Videos ] tab pills | spend "🖼 128 · ~$9.41 / $300 · credits ↗" | Guide | ⚙ Settings ┐
├ LEFT PANEL (380px, collapsible ⟨) ┬ MAIN: media grid (virtualized, fills height, own scroll) ┤
│  tab-specific import panel        │   floating View button (top-right of grid): columns 3–10, hover-previews toggle
│                                   │   selection bar (appears when >0 selected)
├ GENERATE BAR (sticky bottom): settings row · cost estimate · validation state · [⚡ Generate N] ┤
└ run cards stack: bottom-right, floating ┘
```

### 9.3 Left panel — Images tab
Two sections:
1. **Prompts** — header "Prompts — 24" + buttons `Import .txt` (file input) and `Paste` (opens an overlay with a big textarea + "Parse & save"). Below: the prompt list; each row = number badge `#7`, editable text (click to edit, textarea grows, blur/Ctrl+Enter saves via PATCH, Esc cancels), a per-prompt status chip on the right (see 9.7), and on hover a small ✕ (DELETE prompt, confirm if it has media: "Remove prompt 7 from the list? Its generated media stays."). Parse errors/warnings render in a red/amber box above the list; errors keep the old list.
2. **References** — header "References — 31" + `Add references` (multi file input) + a full-width dashed drop zone ("Drop images named 7.png / 7-2.png"). Content: rows grouped by prompt number: `#7  [thumb][thumb][thumb]` — each ref thumb 44px, ordered by slot, hover shows `@1`/`@2` slot label + ✕ to delete. Refs whose number has no prompt in the list render with an amber border + tooltip "No prompt 12 in the list".

### 9.4 Left panel — Videos tab
1. **Prompts** — identical component, bound to prompts-videos.json.
2. **Start frames** — `Add frames` + drop zone ("Drop start frames named 12.png — one per prompt"). Content: grid of frame thumbs (64px) each with its `#12` badge; amber border when no matching prompt; hover ✕ deletes. Below, an auto-computed match summary line: `✓ 22 matched · ⚠ 2 prompts without frame (4, 9) · ⚠ 1 frame without prompt (31)` (numbers listed, truncate with "…" past 8).

Drag-drop: dropping files anywhere over the LEFT PANEL routes to refs (Images tab) / frames (Videos tab). Dropping over the grid does nothing.

### 9.5 Generate bar
Images tab: `Model ▾ (3 Nano Banana models) · Ratio ▾ (11 choices + tiny live ratio-preview box, ported ratio_preview visual) · Resolution ▾ 1K/2K/4K · Outputs ▾ 1–10 · cost — [⚡ Generate 48]`.
Videos tab: `Model ▾ (7 video models) · Ratio ▾ 16:9/9:16 · Resolution ▾ 720p/1080p (hidden for Omni & Veo 2) · Seconds ▾ (model's `durs`, snap on model change) · Sound ☑ (hidden when model lacks audio) · Outputs ▾ 1–3 · cost — [⚡ Generate 44]`.
- Cost text (JetBrains Mono): images `≈ $3.22 · 48 images · $291 left of your $300`; videos `≈ $52.80 · 44 clips × 8s · $291 left`. Recomputed client-side from the ported price tables on every relevant change; "left" comes from usage in state.
- The Generate button label always shows the real job count (matched×outputs for videos). Disabled with reason-tooltip when validation fails; skipped-prompt info renders as an amber note in the bar, not a blocker.
- Every control persists to config on change (`POST /api/config`).

### 9.6 Media grid
- Shows the current tab's media only (images/ or videos/), sorted `(n asc, seq asc)`. Tile = square-ish card, `object-fit: cover` thumb, top-left number badge `#7`, videos also get a duration badge `8s` bottom-right and a subtle ▶ overlay.
- **Virtualization (mandatory):** windowed rendering — compute rows from scroll position, render only visible rows ±2 buffer rows into a spacer-padded container. Target: 1,000 tiles scroll at 60fps. No library; ~80 lines of JS.
- Images: `<img src=/thumb/...?v=mtime loading=lazy>`. Videos: poster `<img>`; on mouseenter (only if hover_previews and `has_preview`) swap in a `<video muted loop autoplay preload=none src=/preview/...?v=>`; on mouseleave remove the element entirely. **Never** more than one preview video playing at once. **Never** load `/media/` full files in the grid.
- Hover action buttons (top-right of tile, Bananza's mkBtn style — small dark pills): `🔄` Regenerate · `✨` New · `📋` Copy · `🗑` Delete. Tooltips exactly: "Regenerate — replace this one", "New — same generation, added next to it", images "Copy image", videos "Copy prompt text", "Delete".
- Regenerate flow: confirm dialog "Regenerate and replace {name}? The current file moves to Trash once the new one succeeds." → POST → tile shows a violet spinner overlay + "regenerating…" (matched via the run's `replaces` field) until version bump refreshes state; the tile then shows the new file (cache-busted by mtime). New: no confirm; tile-sized ghost placeholder with spinner appears right after the original (insert locally) until refresh replaces it with the real tile.
- Delete: single = immediate (toast "Deleted {name} — recoverable in Trash"); the button acts on the whole selection when the tile is part of one.

### 9.7 Per-prompt status chips (left panel, both tabs)
Chip on each prompt row, computed from state: `n✓` count of that prompt's media in the current tab (grey when 0, violet when >0); while a run has pending jobs for that prompt: pulsing `…`; if the latest run recorded failures for it: red `n✗` with tooltip listing the first error. Clicking a chip scrolls the grid to that prompt's first tile.

### 9.8 Selection (port Bananza's behavior to the custom grid)
- Click tile = open lightbox (selection NOT changed by plain click). Ctrl/Cmd+click toggles one; Shift+click selects the range from the last anchor; drag on empty grid space draws a violet lasso rectangle, selecting every intersecting tile (live while dragging); Ctrl+A select all (when grid focused / lightbox closed); Esc clears; Del deletes selection (confirm when ≥2: "Delete the N selected items? They move to Trash — recoverable."). Selected tiles: 2px `#a78bfa` outline + slight brightness.
- Selection bar floats bottom-center above the generate bar when >0: `12 selected · [Delete] · [Clear]`.
- Ctrl+C with exactly one image selected copies it (same pipeline as the Copy button).

### 9.9 Lightbox (custom, ported behavior)
Fullscreen dark overlay. Image: full `/media/` file, wheel-zoom toward cursor, double-click toggle 1×/2.5×, drag-pan when zoomed (port Bananza's zoom math). Video: `<video controls autoplay preload="auto">` full quality with audio. Chrome: filename + `#7` + prompt caption (truncated 160 chars, full on hover title) bottom; buttons top-right: Regenerate · New · Copy · Download · 🗑 Delete · ✕. ←/→ navigate (follow grid order), Del deletes (then advances), Esc closes. Regenerate/New from the lightbox behave exactly like the tile buttons (lightbox stays open on the same slot for Regenerate and shows the spinner state).

### 9.10 Copy implementation
Images: fetch full media → draw to canvas → `canvas.toBlob('image/png')` → `navigator.clipboard.write([new ClipboardItem({'image/png': blob})])` (Bananza's re-encode approach; toast "Image copied"). Videos: `navigator.clipboard.writeText(meta.prompt)` → toast "Prompt copied". Clipboard failures toast the error ("Copy needs Chrome/Edge on localhost — it's blocked here.").

### 9.11 Settings modal (⚙) — one scrollable modal, Bananza's structure rebranded
Sections in order: **Google Cloud connection** (Project ID, Location, Save, Test connection with the ported hint messages rendered as the status panel) · **Performance** (workers slider 1–8 with Bananza's exact info text; "Hover previews for videos" toggle with info "Builds small silent preview clips in the background — turn off to save disk/CPU") · **Trash** (heading with live count; gallery of trash thumbs — posters for videos; select one → Restore / Delete forever; Empty trash with inline confirm row — all ported flows) · **Spend tracker** (the four stat cards: items this session / est. cost this session / items all time / est. cost all time, plus the list-price footnote text ported verbatim, and Reset spend tracker). First launch (no `project` in config) auto-opens Settings, exactly like Bananza. `Guide` button opens the setup-guide modal (ported SETUP_GUIDE text).

### 9.12 Run cards (bottom-right stack, ported look)
Active: `⏳ Run 3 · Images · 24 prompts × 2 → 📁 MyProj` + `31/48 · 4m 12s · ~2m 08s left · 1 failed` + progress bar (indeterminate shimmer at 0) + `✖ Stop`. Finished: green `✅ Run 3 · … · 48/48 ok` or amber with first error snippet + `[Retry 3 failed]`. Finished cards keep the last 3, each dismissible ✕.

### 9.13 Keyboard shortcuts (desktop)
Ctrl+Enter = Generate (works anywhere, including inside textareas) · Ctrl+A / Esc / Del as in 9.8 · ←/→/Esc/Del in lightbox · Ctrl+C copy (9.8). **All grid shortcuts (Ctrl+A, Del, Esc-clear-selection, Ctrl+C) are suppressed whenever `document.activeElement` is an input, textarea, or contenteditable** — typing in a prompt must never select or delete media. Esc while editing a prompt only cancels that edit. No Alt shortcuts, no mobile touch handlers.

### 9.14 Polling loop
`setInterval(2000)`: `GET /api/poll?since=v` → if changed, `GET /api/state` and diff-render (update grid data, run cards, chips, usage, trash count). Re-render must preserve scroll position and selection (reconcile by filename). Pause polling while the tab is hidden (`document.hidden`).

---

## 10. EDGE CASES — HANDLE EXACTLY THESE

1. Filename collision on `gav######` → re-roll (server, §6.2).
2. Regenerate while the SAME file already has a regenerate in flight → 400 "Already regenerating this item."
3. Regenerate/New when meta is missing (hand-copied file) → 400 "No saved recipe for this item."
4. Prompt deleted after media generated → media stays; tiles keep their `#n` badge; chip disappears with the prompt row.
5. Ref/frame deleted after generation → old media unaffected; future Regenerate of dependent items errors per §6.5.
6. Two runs generating into the same project simultaneously → allowed (runs stack, ported model); seq assignment reads max-seq under the meta lock.
7. `.thumbs/` deleted mid-session → lazily rebuilt on request.
8. Video preview not yet built → 404 → frontend keeps poster silently.
9. App restarted mid-run → unfinished runs are simply gone (RUNS is in-memory, ported behavior); finished files persist; no resume feature.
10. Project folder renamed on disk while running → next state call lists it as a new project; acceptable, do nothing special.
11. `usage.json`/`config.json` unwritable → toast the ported warning once, keep running.
12. Non-media junk files inside images//videos/ → ignored by listing (extension whitelist).
13. Windows path length: output names are short by construction; still wrap file writes in try/except → run error, not a crash.
14. `test_connection` while gcloud not installed/authed → the ported hint strings.
15. 4K size rejected by a model → ported `image_size` fallback already handles it.
16. Veo duration typed/stale → `snap_dur` on the server regardless of client state.
17. Uploading 200 refs at once → single POST, single summary toast ("Added 191 · replaced 6 · skipped 3: …").
18. Prompt text containing `@` emails etc. — tags only match `@digits`; `@1` in a no-ref prompt is a validation error (§6.4b) — the user must remove it or add refs.

---

## 11. BUILD PHASES (do in order; verify each)

**P1 — Scaffold & boot.** requirements.txt, package layout, config.py, models_tables.py (ported), FastAPI serving static shell + `/api/boot` + cache middleware, run_megagen.py console experience (banner, loader, auto-open browser, crash handler). *Verify:* `python run_megagen.py` shows the branded console, opens the browser, dark page renders with topbar + empty panels.
**P2 — Projects, prompts, uploads.** projects.py, prompts.py parser + matchers + tests-by-hand, all project/prompt/ref/frame endpoints + persistence, trash plumbing. *Verify:* curl a numbered list in → parsed JSON back; upload `7-2.png` → appears in refs listing; malformed names → skipped list.
**P3 — Generation engine.** gen_engine.py ports + job builders + RUNS + usage.py + regenerate/new/retry/cancel endpoints. *Verify:* with a real Project ID: 3-prompt list × 2 outputs generates 6 correctly named files in order, .meta.json recipes present, usage increments; a video prompt+frame pair yields `NVid-gav… .mp4`.
**P4 — Left panel + generate bar UI.** Prompt list w/ inline edit, refs/frames sections, drop zones, validation surfaces, settings controls with persistence, cost estimate, Generate wiring, run cards, poll loop.
**P5 — Grid + media serving.** thumbs.py, media/thumb/preview endpoints (Range verified via `curl -H "Range: bytes=0-99"` → expect 206), virtualized grid, badges, hover previews, hover actions, selection + lasso + shortcuts, selection bar, per-prompt chips.
**P6 — Lightbox, copy, trash UI, settings modal, polish.** Everything in 9.9–9.12, confirm dialogs, toasts, empty states ("Import prompts to get started" center-panel hint), README.
*Final verify:* walk the acceptance checklist below top to bottom.

## 12. ACCEPTANCE CHECKLIST (all must pass)

- [ ] Import a 20-prompt .txt with `1.`…`20.` forms → list renders sorted; duplicate-number file → error, old list intact.
- [ ] Bulk-add `1-1.png 1-2.png 2.png junk.png` → refs 1@1 1@2 2@1, junk skipped with toast.
- [ ] `@2` in a 1-ref prompt blocks Generate with the exact error message.
- [ ] Generate 20×3 images → 60 files `n-gav######.png`, grid ordered by n, badges correct, cost tracked.
- [ ] Videos: frames 1–10 + prompts 1–12 → summary shows 2 promptless-frame/frameless-prompt warnings, Generate makes 10×outputs clips named `nVid-gav######.mp4`.
- [ ] Regenerate keeps filename & grid position; original lands in Trash; failure leaves original untouched.
- [ ] New inserts directly after its original and both remain.
- [ ] Copy on an image puts a PNG on the clipboard; Copy on a video puts the prompt text.
- [ ] Drag-lasso, Ctrl/Shift-click, Ctrl+A, Esc, Del (+confirm ≥2) all work; Trash restore returns items to the open project with recipes intact (Regenerate works after restore).
- [ ] Grid with 300+ items scrolls smoothly; video tiles show posters only; hover preview is low-res and silent; lightbox video is full quality with sound; seeking works instantly (Range 206 confirmed).
- [ ] Stop halts a big run; Retry failed re-runs only failures into the same filenames.
- [ ] Relaunch restores project, tab, every setting, prompts, refs, frames, media, spend totals.
- [ ] Settings → Test connection shows the correct hint for a bad Project ID.

## 13. OUT OF SCOPE — DO NOT BUILD

Mobile/responsive layouts · `--share`/tunnel links · end frames or Frames/Ingredients modes · text-to-video · per-prompt output overrides · multi-select regenerate · databases · authentication · image editing tools · Gradio · any framework or build tooling.
