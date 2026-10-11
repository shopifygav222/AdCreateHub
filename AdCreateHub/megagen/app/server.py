"""FastAPI app: JSON API, media serving with Range, static frontend."""
import mimetypes
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import accounts as accounts_mod
from . import gen_engine, projects, prompts as prompts_mod, thumbs, usage
from .config import (DEFAULT_PROJECT, NOTICES, STATIC_DIR, ensure_dirs, load_config,
                     update_config)
from .gen_engine import GenError
from .models_tables import tables_payload

app = FastAPI(title="MegaGen", docs_url=None, redoc_url=None, openapi_url=None)

CACHE_PREFIXES = ("/media/", "/thumb/", "/preview/", "/trash-thumb/", "/trash-media/")
CHUNK = 512 * 1024


# ---------------- middleware ----------------

@app.middleware("http")
async def media_cache(request: Request, call_next):
    """Ported from _media_cache_app_kwargs: media is immutable per mtime, and the
    frontend always cache-busts with ?v={mtime}, so a week of caching is safe."""
    resp = await call_next(request)
    if request.url.path.startswith(CACHE_PREFIXES) and resp.status_code < 400:
        resp.headers["Cache-Control"] = "public, max-age=604800"
    elif request.url.path.startswith("/static/"):
        # revalidate the app's own JS/CSS every load (cheap — StaticFiles sends an
        # ETag) so an updated MegaGen never runs against a stale cached frontend
        resp.headers["Cache-Control"] = "no-cache"
    return resp


def fail(msg, status=400, **extra):
    payload = {"error": str(msg)}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


async def body(request: Request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _project_of(data, key="project"):
    return projects.valid_project(data.get(key) or load_config().get("last_project"))


# ---------------- state & polling ----------------

@app.get("/api/boot")
def api_boot():
    ensure_dirs()
    projects.project_dir(DEFAULT_PROJECT)
    return {"config": load_config(), "projects": projects.list_projects(),
            "version": gen_engine.data_version(), "tables": tables_payload()}


@app.get("/api/state")
def api_state(project: str = "", tab: str = "images"):
    p = projects.valid_project(project)
    tab = "videos" if tab == "videos" else "images"
    refs = prompts_mod.list_refs(p) if tab == "images" else {}
    frames = prompts_mod.list_frames(p) if tab == "videos" else {}
    return {
        "project": p,
        "tab": tab,
        "projects": projects.list_projects(),
        "prompts": prompts_mod.load_prompts(p, tab),
        "refs": {str(k): v for k, v in refs.items()},
        "frames": {str(k): v for k, v in frames.items()},
        "media": projects.list_media(p, tab, thumbs),
        "runs": gen_engine.runs_payload(),
        "usage": usage.usage_payload(),
        "trash_count": projects.trash_count(),
        "notices": list(NOTICES),
        "version": gen_engine.data_version(),
    }


@app.get("/api/poll")
def api_poll(since: int = -1):
    return {"version": gen_engine.data_version()}


# ---------------- projects ----------------

@app.post("/api/projects")
async def api_project_create(request: Request):
    data = await body(request)
    try:
        name = projects.create_project(data.get("name"))
    except ValueError as e:
        return fail(e)
    update_config({"last_project": name})
    gen_engine.bump()
    return {"ok": True, "name": name, "projects": projects.list_projects()}


@app.delete("/api/projects/{name}")
def api_project_delete(name: str):
    try:
        projects.delete_project(name)
    except ValueError as e:
        return fail(e)
    update_config({"last_project": DEFAULT_PROJECT})
    gen_engine.bump()
    return {"ok": True, "projects": projects.list_projects(), "project": DEFAULT_PROJECT}


@app.post("/api/projects/{name}/open_folder")
def api_project_open(name: str):
    return {"ok": True, "message": projects.open_output_folder(projects.valid_project(name))}


# ---------------- prompts ----------------

@app.post("/api/prompts")
async def api_prompts_import(request: Request):
    data = await body(request)
    p = _project_of(data)
    tab = "videos" if data.get("tab") == "videos" else "images"
    parsed, warnings, errors = prompts_mod.parse_prompts(data.get("text"))
    if errors:
        return {"prompts": prompts_mod.load_prompts(p, tab), "warnings": warnings,
                "errors": errors, "saved": False}
    saved = prompts_mod.save_prompts(p, tab, parsed)
    gen_engine.bump()
    return {"prompts": saved, "warnings": warnings, "errors": [], "saved": True}


@app.patch("/api/prompts")
async def api_prompt_patch(request: Request):
    data = await body(request)
    p = _project_of(data)
    tab = "videos" if data.get("tab") == "videos" else "images"
    try:
        saved = prompts_mod.patch_prompt(p, tab, data.get("n"), data.get("text"))
    except (ValueError, TypeError) as e:
        return fail(e)
    gen_engine.bump()
    return {"ok": True, "prompts": saved}


@app.post("/api/prompts/clear")
async def api_prompts_clear(request: Request):
    data = await body(request)
    p = _project_of(data)
    tab = "videos" if data.get("tab") == "videos" else "images"
    n = len(prompts_mod.load_prompts(p, tab))
    prompts_mod.save_prompts(p, tab, [])
    gen_engine.bump()
    return {"ok": True, "cleared": n}


@app.delete("/api/prompts")
async def api_prompt_delete(request: Request):
    data = await body(request)
    p = _project_of(data)
    tab = "videos" if data.get("tab") == "videos" else "images"
    try:
        saved = prompts_mod.delete_prompt(p, tab, data.get("n"))
    except (ValueError, TypeError) as e:
        return fail(e)
    gen_engine.bump()
    return {"ok": True, "prompts": saved}


# ---------------- refs & frames ----------------

async def _upload(request: Request, kind):
    form = await request.form()
    p = projects.valid_project(form.get("project") or load_config().get("last_project"))
    uploads = []
    for value in form.getlist("files"):
        filename = getattr(value, "filename", None)
        if not filename:
            continue
        uploads.append((filename, await value.read()))
    if not uploads:
        return fail("No files arrived.")
    result = prompts_mod.save_uploads(p, uploads, kind)
    gen_engine.bump()
    return result


@app.post("/api/refs")
async def api_refs_add(request: Request):
    return await _upload(request, "refs")


@app.post("/api/frames")
async def api_frames_add(request: Request):
    return await _upload(request, "frames")


@app.post("/api/refs/clear")
async def api_refs_clear(request: Request):
    data = await body(request)
    n = prompts_mod.clear_uploads(_project_of(data), "refs")
    gen_engine.bump()
    return {"ok": True, "cleared": n}


@app.post("/api/frames/clear")
async def api_frames_clear(request: Request):
    data = await body(request)
    n = prompts_mod.clear_uploads(_project_of(data), "frames")
    gen_engine.bump()
    return {"ok": True, "cleared": n}


@app.delete("/api/refs")
async def api_refs_delete(request: Request):
    data = await body(request)
    prompts_mod.delete_upload(_project_of(data), data.get("name"), "refs")
    gen_engine.bump()
    return {"ok": True}


@app.delete("/api/frames")
async def api_frames_delete(request: Request):
    data = await body(request)
    prompts_mod.delete_upload(_project_of(data), data.get("name"), "frames")
    gen_engine.bump()
    return {"ok": True}


# ---------------- generation ----------------

@app.post("/api/generate")
async def api_generate(request: Request):
    data = await body(request)
    p = _project_of(data)
    kind = data.get("kind")
    settings = data.get("settings") or {}
    try:
        if kind == "video":
            out = gen_engine.start_videos(p, settings)
        else:
            out = gen_engine.start_images(p, settings)
    except GenError as e:
        errs = e.args[0] if e.args and isinstance(e.args[0], list) else [str(e)]
        return JSONResponse({"error": errs[0], "errors": errs}, status_code=400)
    except Exception as e:
        return fail(f"{type(e).__name__}: {e}")
    return out


@app.post("/api/media/regenerate")
async def api_regenerate(request: Request):
    data = await body(request)
    try:
        return gen_engine.regenerate(_project_of(data), data.get("name"))
    except GenError as e:
        return fail(e)
    except Exception as e:
        return fail(f"{type(e).__name__}: {e}")


@app.post("/api/media/new")
async def api_new(request: Request):
    data = await body(request)
    try:
        return gen_engine.new_from(_project_of(data), data.get("name"))
    except GenError as e:
        return fail(e)
    except Exception as e:
        return fail(f"{type(e).__name__}: {e}")


@app.post("/api/media/edit")
async def api_media_edit(request: Request):
    data = await body(request)
    try:
        return gen_engine.edit_from(_project_of(data), data.get("name"),
                                    data.get("prompt"), data.get("outputs"))
    except GenError as e:
        return fail(e)
    except Exception as e:
        return fail(f"{type(e).__name__}: {e}")


@app.post("/api/media/delete")
async def api_media_delete(request: Request):
    data = await body(request)
    names = [str(n) for n in (data.get("names") or [])]
    gone = gen_engine.delete_media(_project_of(data), names)
    return {"ok": True, "deleted": gone}


@app.post("/api/runs/{run_id}/retry_failed")
def api_retry(run_id: int):
    try:
        return gen_engine.retry_failed(run_id)
    except GenError as e:
        return fail(e)
    except Exception as e:
        return fail(f"{type(e).__name__}: {e}")


@app.post("/api/runs/{run_id}/cancel")
def api_cancel(run_id: int):
    try:
        gen_engine.cancel_run(run_id)
    except GenError as e:
        return fail(e)
    return {"ok": True}


@app.post("/api/runs/{run_id}/dismiss")
def api_dismiss(run_id: int):
    gen_engine.dismiss_run(run_id)
    return {"ok": True}


# ---------------- trash ----------------

@app.get("/api/trash")
def api_trash():
    return projects.list_trash()


@app.post("/api/trash/restore")
async def api_trash_restore(request: Request):
    data = await body(request)
    try:
        name = projects.restore_trash(data.get("name"), _project_of(data))
    except ValueError as e:
        return fail(e)
    gen_engine.bump()
    return {"ok": True, "name": name}


@app.post("/api/trash/purge")
async def api_trash_purge(request: Request):
    data = await body(request)
    try:
        projects.purge_trash(data.get("name"))
    except ValueError as e:
        return fail(e)
    gen_engine.bump()
    return {"ok": True}


@app.post("/api/trash/empty")
def api_trash_empty():
    purged, locked = projects.empty_trash()
    gen_engine.bump()
    return {"ok": True, "purged": purged, "locked": locked}


# ---------------- settings ----------------

@app.post("/api/config")
async def api_config(request: Request):
    data = await body(request)
    cfg = update_config(data)
    return {"ok": True, "config": cfg}


@app.get("/api/accounts")
def api_accounts():
    return {"accounts": accounts_mod.health_payload(),
            "usage": usage.usage_payload()}


@app.post("/api/accounts")
async def api_account_add(request: Request):
    data = await body(request)
    accts = accounts_mod.list_accounts()
    acct = {"id": accounts_mod.next_account_id(),
            "label": (str(data.get("label") or "").strip() or f"Account {len(accts) + 1}")[:40],
            "project": str(data.get("project") or "").strip(),
            "location": (str(data.get("location") or "").strip() or "global"),
            "auth": "adc", "credit": 0.0, "enabled": True}
    accts.append(acct)
    accounts_mod.save_accounts(accts)
    gen_engine.bump()
    return {"ok": True, "id": acct["id"], "accounts": accounts_mod.health_payload()}


@app.patch("/api/accounts/{acct_id}")
async def api_account_patch(acct_id: str, request: Request):
    data = await body(request)
    accts = accounts_mod.list_accounts()
    hit = next((a for a in accts if a["id"] == acct_id), None)
    if not hit:
        return fail("That account is gone already.")
    if "label" in data:
        hit["label"] = (str(data["label"]).strip() or hit["label"])[:40]
    if "project" in data:
        hit["project"] = str(data["project"]).strip()
    if "location" in data:
        hit["location"] = str(data["location"]).strip() or "global"
    if "enabled" in data:
        hit["enabled"] = bool(data["enabled"])
    if "credit" in data:
        try:
            hit["credit"] = max(0.0, float(data["credit"]))
        except (TypeError, ValueError):
            pass
    accounts_mod.save_accounts(accts)
    accounts_mod.forget_client(acct_id)      # project/location may have changed
    gen_engine.bump()
    return {"ok": True, "accounts": accounts_mod.health_payload()}


@app.delete("/api/accounts/{acct_id}")
def api_account_delete(acct_id: str):
    accts = accounts_mod.list_accounts()
    if len(accts) <= 1:
        return fail("Keep at least one account.")
    left = [a for a in accts if a["id"] != acct_id]
    if len(left) == len(accts):
        return fail("That account is gone already.")
    accounts_mod.save_accounts(left)
    accounts_mod.forget_client(acct_id)
    gen_engine.bump()
    return {"ok": True, "accounts": accounts_mod.health_payload()}


@app.post("/api/accounts/{acct_id}/test")
def api_account_test(acct_id: str):
    acct = accounts_mod.find_account(acct_id)
    if not acct:
        return fail("That account is gone already.")
    ok, message = accounts_mod.test_account(acct)
    gen_engine.bump()
    return {"ok": ok, "message": message}


@app.post("/api/usage/reset")
def api_usage_reset():
    usage.reset_tracker()
    gen_engine.bump()
    return {"ok": True, "usage": usage.usage_payload()}


@app.post("/api/notices/clear")
def api_notices_clear():
    NOTICES.clear()
    return {"ok": True}


# ---------------- media serving ----------------

def _send(path: Path, request: Request, media_type=None):
    """FileResponse for whole-file GETs, a hand-rolled 206 for Range requests so
    lightbox video seeks land instantly on every Starlette version."""
    if not path or not path.exists() or not path.is_file():
        return Response(status_code=404)
    media_type = media_type or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    stat = path.stat()
    size = stat.st_size
    etag = f'"{int(stat.st_mtime)}-{size}"'
    common = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": time.strftime("%a, %d %b %Y %H:%M:%S GMT",
                                       time.gmtime(stat.st_mtime)),
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=common)

    rng = (request.headers.get("range") or "").strip()
    if rng.startswith("bytes=") and "," not in rng:
        spec = rng[6:].strip()
        try:
            first, _, last = spec.partition("-")
            if first == "":                       # suffix range: bytes=-500
                length = min(int(last), size)
                start, end = size - length, size - 1
            else:
                start = int(first)
                end = int(last) if last else size - 1
            end = min(end, size - 1)
            if start > end or start >= size:
                raise ValueError
        except ValueError:
            return Response(status_code=416,
                            headers={**common, "Content-Range": f"bytes */{size}"})

        def stream():
            remaining = end - start + 1
            with open(path, "rb") as fh:
                fh.seek(start)
                while remaining > 0:
                    chunk = fh.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            stream(), status_code=206, media_type=media_type,
            headers={**common, "Content-Range": f"bytes {start}-{end}/{size}",
                     "Content-Length": str(end - start + 1)})

    return FileResponse(path, media_type=media_type, headers=common)


@app.get("/media/{project}/{name}")
def api_media(project: str, name: str, request: Request):
    p = projects.find_media(projects.valid_project(project), Path(name).name)
    return _send(p, request)


@app.get("/thumb/{project}/{name}")
def api_thumb(project: str, name: str, request: Request):
    p = thumbs.ensure_thumb(projects.valid_project(project), Path(name).name)
    return _send(p, request)


@app.get("/preview/{project}/{name}")
def api_preview(project: str, name: str, request: Request):
    proj = projects.valid_project(project)
    p = thumbs.get_preview(proj, Path(name).name)
    if not p:
        thumbs.queue_preview(proj, Path(name).name)
        return Response(status_code=404)      # the grid just keeps its poster
    return _send(p, request, "video/mp4")


@app.get("/ref/{project}/{kind}/{name}")
def api_ref_file(project: str, kind: str, name: str, request: Request):
    proj = projects.valid_project(project)
    d = projects.frames_dir(proj) if kind == "frames" else projects.refs_dir(proj)
    return _send(d / Path(name).name, request)


@app.get("/trash-thumb/{name}")
def api_trash_thumb(name: str, request: Request):
    return _send(thumbs.ensure_trash_thumb(Path(name).name), request)


@app.get("/trash-media/{name}")
def api_trash_media(name: str, request: Request):
    """Full-quality media straight from the trash, for the §C4 viewer."""
    return _send(projects.trash_path(Path(name).name), request)


# ---------------- frontend ----------------

@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-store"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
