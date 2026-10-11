"""Numbered prompt-list parser, reference/start-frame filename matchers, @tags."""
import json
import re
import time
from pathlib import Path

from .models_tables import TAG_RE
from .projects import project_dir, refs_dir, frames_dir, preserve_input

MAX_PROMPTS = 500
MAX_REF_SLOT = 6
UPLOAD_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# `1. text` · `1) text` · `1: text`
NUM_RE = re.compile(r"^\s*(\d{1,4})\s*[\.\)\:]\s*(.*)$")
# `1 - text`
DASH_RE = re.compile(r"^\s*(\d{1,4})\s+-\s+(.*)$")

REF_STEM_RE = re.compile(r"^(\d{1,4})(?:[-_ ](\d{1,2}))?$")
FRAME_STEM_RE = re.compile(r"^(\d{1,4})$")


# ---------------- parser (§5) ----------------

def parse_prompts(raw):
    """(prompts, warnings, errors). Errors block saving; warnings do not."""
    warnings, errors = [], []
    text = (raw or "").replace("﻿", "").replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    items = []          # {"n", "lines": [str], "line": 1-based source line}
    pre_lines = 0
    for idx, line in enumerate(lines):
        m = NUM_RE.match(line) or DASH_RE.match(line)
        if m:
            items.append({"n": int(m.group(1)), "lines": [m.group(2)], "line": idx + 1})
        elif items:
            items[-1]["lines"].append(line)
        elif line.strip():
            pre_lines += 1

    if not items:
        # no numbering anywhere → blank-line blocks, auto-numbered 1..N
        blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
        if not blocks:
            return [], warnings, ["Nothing to import — the list is empty."]
        items = [{"n": i + 1, "lines": [b], "line": 1} for i, b in enumerate(blocks)]
        warnings.append(f"No numbering found — prompts were auto-numbered 1–{len(blocks)}.")
    elif pre_lines:
        warnings.append(f"Ignored {pre_lines} line(s) before the first numbered prompt.")

    kept = []
    for it in items:
        body = "\n".join(it["lines"]).strip()
        if not body:
            warnings.append(f"Prompt {it['n']} is empty — it was skipped.")
            continue
        kept.append({"n": it["n"], "text": body, "line": it["line"]})

    seen = {}
    for it in kept:
        if it["n"] in seen:
            errors.append(f"Duplicate prompt number {it['n']} "
                          f"(lines {seen[it['n']]} and {it['line']}).")
        else:
            seen[it["n"]] = it["line"]

    if len(kept) > MAX_PROMPTS:
        errors.append(f"That's {len(kept)} prompts — the limit is {MAX_PROMPTS} per list.")

    if errors:
        return [], warnings, errors

    kept.sort(key=lambda i: i["n"])
    return [{"n": i["n"], "text": i["text"]} for i in kept], warnings, errors


# ---------------- storage ----------------

def prompts_file(project, tab):
    tab = "videos" if tab == "videos" else "images"
    return project_dir(project) / f"prompts-{tab}.json"


def load_prompts(project, tab):
    p = prompts_file(project, tab)
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out = []
            for it in (data.get("prompts") or []):
                try:
                    out.append({"n": int(it["n"]), "text": str(it.get("text", ""))})
                except (KeyError, TypeError, ValueError):
                    continue
            out.sort(key=lambda i: i["n"])
            return out
        except Exception:
            pass
    return []


def save_prompts(project, tab, prompts):
    payload = {"prompts": sorted(prompts, key=lambda i: i["n"]), "updated": int(time.time())}
    prompts_file(project, tab).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload["prompts"]


def patch_prompt(project, tab, n, text):
    prompts = load_prompts(project, tab)
    hit = False
    for it in prompts:
        if it["n"] == int(n):
            it["text"] = (text or "").strip()
            hit = True
    if not hit:
        raise ValueError(f"Prompt {n} isn't in the list anymore.")
    return save_prompts(project, tab, prompts)


def delete_prompt(project, tab, n):
    prompts = [p for p in load_prompts(project, tab) if p["n"] != int(n)]
    return save_prompts(project, tab, prompts)


# ---------------- reference images / start frames ----------------

def list_refs(project):
    """{n: [{"slot": 1, "name": "7-1.png"}, …]} — slot order, one file per slot."""
    out = {}
    d = refs_dir(project)
    if not d.is_dir():
        return out
    for f in sorted(d.iterdir(), key=lambda p: p.name):
        if not (f.is_file() and f.suffix.lower() in UPLOAD_EXTS):
            continue
        m = REF_STEM_RE.match(f.stem)
        if not m:
            continue
        n = int(m.group(1))
        slot = int(m.group(2) or 1)
        out.setdefault(n, []).append({"slot": slot, "name": f.name})
    for n in out:
        out[n].sort(key=lambda r: r["slot"])
    return out


def ref_paths(project, n):
    """Absolute paths of prompt n's references, in slot order."""
    d = refs_dir(project)
    return [str(d / r["name"]) for r in list_refs(project).get(int(n), [])]


def list_frames(project):
    """{n: "12.png"} — one start frame per prompt, always."""
    out = {}
    d = frames_dir(project)
    if not d.is_dir():
        return out
    for f in sorted(d.iterdir(), key=lambda p: p.name):
        if not (f.is_file() and f.suffix.lower() in UPLOAD_EXTS):
            continue
        m = FRAME_STEM_RE.match(f.stem)
        if m:
            out[int(m.group(1))] = f.name
    return out


def frame_path(project, n):
    name = list_frames(project).get(int(n))
    return str(frames_dir(project) / name) if name else None


def _save_one_ref(project, stem, ext, data):
    m = REF_STEM_RE.match(stem)
    if not m:
        return None, "name"
    n = int(m.group(1))
    slot = int(m.group(2) or 1)
    if slot < 1 or slot > MAX_REF_SLOT:
        return None, "slot"
    d = refs_dir(project)
    # One file per (prompt, slot) — a re-upload replaces whatever was in that slot,
    # including the same slot saved under a different extension. Refs are user
    # inputs, so the old file is deleted outright, never trashed.
    replaced = False
    for old in list(d.glob(f"{n}-{slot}.*")):
        if old.is_file():
            old.unlink(missing_ok=True)
            replaced = True
    dest = d / f"{n}-{slot}{ext}"
    dest.write_bytes(data)
    return {"n": n, "slot": slot, "name": dest.name, "replaced": replaced}, None


def _save_one_frame(project, stem, ext, data):
    m = FRAME_STEM_RE.match(stem)
    if not m:
        return None, "name"
    n = int(m.group(1))
    d = frames_dir(project)
    replaced = False
    for old in list(d.glob(f"{n}.*")):     # one frame per prompt, across extensions
        if old.is_file():
            old.unlink(missing_ok=True)
            replaced = True
    dest = d / f"{n}{ext}"
    dest.write_bytes(data)
    return {"n": n, "slot": 1, "name": dest.name, "replaced": replaced}, None


def save_uploads(project, uploads, kind):
    """uploads: [(filename, bytes)]. kind: "refs" | "frames".
    Returns {added, replaced, skipped:[{name, reason}]} for one summary toast."""
    added, replaced, skipped = 0, 0, []
    for filename, data in uploads:
        p = Path(filename or "")
        ext = p.suffix.lower()
        if ext not in UPLOAD_EXTS:
            skipped.append({"name": p.name, "reason": "ext"})
            continue
        if ext == ".jpeg":
            ext = ".jpg"
        try:
            if kind == "frames":
                info, why = _save_one_frame(project, p.stem.strip(), ext, data)
            else:
                info, why = _save_one_ref(project, p.stem.strip(), ext, data)
        except Exception:
            skipped.append({"name": p.name, "reason": "write"})
            continue
        if info is None:
            skipped.append({"name": p.name, "reason": why})
        elif info["replaced"]:
            replaced += 1
        else:
            added += 1
    return {"added": added, "replaced": replaced, "skipped": skipped}


def clear_uploads(project, kind):
    """Delete every uploaded ref (kind="refs") or start frame (kind="frames").
    Returns how many files were removed. Inputs are never trashed — same rule
    as single-file deletion."""
    d = frames_dir(project) if kind == "frames" else refs_dir(project)
    n = 0
    if d.is_dir():
        for f in list(d.iterdir()):
            if f.is_file() and f.suffix.lower() in UPLOAD_EXTS:
                preserve_input(project, f, kind)
                try:
                    f.unlink(missing_ok=True)
                    n += 1
                except OSError:
                    # Windows locks a file while a worker reads it or the panel
                    # streams its thumbnail — skip it, never abort the whole clear
                    pass
    return n


def delete_upload(project, name, kind):
    d = frames_dir(project) if kind == "frames" else refs_dir(project)
    p = d / Path(str(name)).name
    if p.exists() and p.is_file():
        preserve_input(project, p, kind)
        p.unlink(missing_ok=True)
        return True
    return False


# ---------------- @tags (§5.3) ----------------

def prompt_tags(text):
    return [int(t) for t in TAG_RE.findall(text or "")]


def tag_errors(prompts, refs):
    """Pre-flight (§6.4b): every @j must exist as a reference slot for that prompt."""
    errs = []
    for it in prompts:
        k = len(refs.get(it["n"], []))
        for j in prompt_tags(it["text"]):
            if j < 1 or j > k:
                errs.append(f"Prompt {it['n']} uses @{j} but has only {k} reference(s).")
                break
    return errs


def build_prompt_final(text, k):
    """Rewrite @j → "image j" and, when the prompt has references, prepend the
    same primer the reference app's plan_jobs uses. All k refs are attached in
    slot order whether or not the text tags them."""
    clean = TAG_RE.sub(lambda m: f"image {m.group(1)}", text or "")
    if k > 0:
        order = ", ".join(f"image {i}" for i in range(1, k + 1))
        clean = f"You are given {k} reference image(s), in order: {order}. " + clean
    return clean
