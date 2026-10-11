"""
AdCreateHub Remote Control Panel
================================
Runs 24/7 on your Oracle Cloud server. Gives you a password-protected,
mobile-first webpage with power buttons for BOTH Bananza AND MegaGen.
Press either and that app starts; when it's online you get an Open link
straight to it.

Bananza is served on port 7860, MegaGen on port 7861 — both must be open
in your Oracle Cloud security list AND in the server firewall, just like
port 8000 (this panel) already is.

Change SECRET_KEY below, then:
    sudo systemctl restart adcreatehub-panel
"""

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, request

# ----------------------------------------------------------------------------
# SETTINGS
# ----------------------------------------------------------------------------
SECRET_KEY = "minion01"                # password for the control page
PORT = 8000                            # the port the control page lives on
START_TIMEOUT_SECONDS = 240            # give up if an app isn't up after 4 min
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent


class AppManager:
    """One instance per app we can start — Bananza or MegaGen. Each lives in
    its own subfolder and listens on its own port; the Hub never cares what
    the apps do, only whether they are up on the port they said they'd use."""

    def __init__(self, key, name, folder, entry, port, env_extra):
        self.key = key                 # short id used in URLs/JSON
        self.name = name               # display name
        self.folder = BASE_DIR / folder
        self.entry = entry             # python file to run
        self.port = port
        self.env_extra = env_extra     # extra env vars to pass the child
        self._lock = threading.Lock()
        self._state = "offline"        # offline | starting | online | stopping | error
        self._error_msg = None
        self._proc = None
        self._started_at = None
        self._log = deque(maxlen=300)

    # ---------- state helpers ----------
    def _set(self, state=None, err="__keep__"):
        with self._lock:
            if state is not None:
                self._state = state
            if err != "__keep__":
                self._error_msg = err

    def snapshot(self):
        with self._lock:
            state, err = self._state, self._error_msg
            up = int(time.time() - self._started_at) \
                if (self._started_at and state == "online") else 0
        tail = list(self._log)[-15:] if state == "error" else []
        return {"key": self.key, "name": self.name, "port": self.port,
                "state": state, "error": err, "uptime": up, "log": tail}

    # ---------- watchers ----------
    def _watch_output(self, proc):
        try:
            for raw in proc.stdout:
                line = raw.rstrip("\r\n")
                if line.strip():
                    self._log.append(line)
        except Exception as e:
            self._log.append(f"[hub] output reader stopped: {e}")
        proc.wait()
        with self._lock:
            was = self._state
        if was == "starting":
            self._set(state="error",
                      err="The app closed while starting up. The last log "
                          "lines below usually say why.")
        elif was in ("online", "stopping"):
            self._set(state="offline", err=None)

    def _watch_ready(self, proc):
        while True:
            time.sleep(1.5)
            with self._lock:
                if self._proc is not proc or self._state != "starting":
                    return
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=2)
            except urllib.error.HTTPError:
                pass                   # any HTTP answer means the server is up
            except Exception:
                continue
            self._set(state="online", err=None)
            self._started_at = time.time()
            return

    def _watch_timeout(self, proc):
        waited = 0
        while waited < START_TIMEOUT_SECONDS:
            time.sleep(2)
            waited += 2
            with self._lock:
                if self._state != "starting" or self._proc is not proc:
                    return
        self._kill(proc)
        self._set(state="error",
                  err=f"The app didn't come online after "
                      f"{START_TIMEOUT_SECONDS} seconds — gave up. "
                      "Press the button to try again.")

    def _kill(self, proc):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(20):
            if proc.poll() is not None:
                return
            time.sleep(0.5)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # ---------- public controls ----------
    def start(self):
        with self._lock:
            if self._state in ("starting", "online", "stopping"):
                return False
        self._log.clear()
        self._set(state="starting", err=None)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", **self.env_extra}
        proc = subprocess.Popen(
            [sys.executable, "-u", str(self.folder / self.entry)],
            cwd=str(self.folder),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            start_new_session=True,
        )
        with self._lock:
            self._proc = proc
        threading.Thread(target=self._watch_output, args=(proc,), daemon=True).start()
        threading.Thread(target=self._watch_ready, args=(proc,), daemon=True).start()
        threading.Thread(target=self._watch_timeout, args=(proc,), daemon=True).start()
        return True

    def stop(self):
        with self._lock:
            proc = self._proc
            stoppable = proc is not None and self._state in ("starting", "online")
        if not stoppable:
            return False
        self._set(state="stopping")
        threading.Thread(target=self._kill, args=(proc,), daemon=True).start()
        return True


# Register the two apps. Each one has to serve on 0.0.0.0 so Oracle's public
# IP can reach it — hence the env vars that each app's launcher honours.
APPS = {
    "bananza": AppManager(
        key="bananza",
        name="Bananza",
        folder="Bananza",
        entry="bulk_banana_gui.py",
        port=7860,
        env_extra={
            "BANANA_HOST": "0.0.0.0",
            "BANANA_PORT": "7860",
            "BANANA_NO_BROWSER": "1",
        },
    ),
    "megagen": AppManager(
        key="megagen",
        name="MegaGen",
        folder="megagen",
        entry="run_megagen.py",
        port=7861,
        env_extra={
            "MEGAGEN_HOST": "0.0.0.0",
            "MEGAGEN_PORT": "7861",
            "MEGAGEN_NO_BROWSER": "1",
        },
    ),
}


# ---------------------------- web endpoints ---------------------------------

def _authorized():
    supplied = request.args.get("key") or request.headers.get("X-Key") or ""
    return supplied == SECRET_KEY


def _lookup(key):
    return APPS.get(key)


app = Flask(__name__)


@app.get("/api/status")
def api_status():
    if not _authorized():
        return jsonify({"error": "bad key"}), 401
    return jsonify({"apps": [a.snapshot() for a in APPS.values()]})


@app.post("/api/<key>/start")
def api_start(key):
    if not _authorized():
        return jsonify({"error": "bad key"}), 401
    a = _lookup(key)
    if not a:
        return jsonify({"error": "unknown app"}), 404
    a.start()
    return jsonify({"ok": True})


@app.post("/api/<key>/stop")
def api_stop(key):
    if not _authorized():
        return jsonify({"error": "bad key"}), 401
    a = _lookup(key)
    if not a:
        return jsonify({"error": "unknown app"}), 404
    a.stop()
    return jsonify({"ok": True})


@app.get("/api/internal/status")
def api_internal_status():
    """Localhost-only, no auth. Used by auto-update.sh to decide whether
    to defer the restart because an app is still running."""
    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({"error": "forbidden"}), 403
    busy = {"starting", "online", "stopping"}
    any_running = any(a.snapshot()["state"] in busy for a in APPS.values())
    return jsonify({"any_running": any_running})


# Flag file the auto-updater touches when it defers a restart.
PENDING_FLAG = BASE_DIR / ".update-pending"


def _cors(response):
    """Both apps load /pending-banner.js from this hub on a different port —
    the browser treats that as cross-origin, so we send permissive headers.
    These two endpoints expose only a boolean, no secrets."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/update-pending")
def api_update_pending():
    return _cors(jsonify({"pending": PENDING_FLAG.exists()}))


@app.get("/pending-banner.js")
def pending_banner_js():
    """Tiny self-contained script included by Bananza and MegaGen. Polls the
    hub every 15 s and shows a floating pill when an update is waiting."""
    return _cors(app.response_class(_PENDING_BANNER_JS, mimetype="application/javascript"))


_PENDING_BANNER_JS = r"""/* adcreatehub pending-update banner */
(function () {
  'use strict';
  var HUB = location.protocol + '//' + location.hostname + ':8000';
  var banner = null;

  function ensure(show) {
    if (!show) { if (banner) banner.style.display = 'none'; return; }
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'adch-update-banner';
      banner.style.cssText =
        'position:fixed;left:50%;top:max(10px,env(safe-area-inset-top,0px));' +
        'transform:translateX(-50%);z-index:2147483647;' +
        'background:linear-gradient(135deg,#f59e0b,#d97706);color:#1a0f04;' +
        'padding:8px 14px;border-radius:999px;' +
        'font:600 12.5px/1.3 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;' +
        'box-shadow:0 10px 28px rgba(0,0,0,.45);' +
        'display:flex;align-items:center;gap:6px;max-width:92vw;' +
        'text-align:center;pointer-events:none;letter-spacing:.2px;';
      banner.innerHTML = '<span style="font-size:14px">⚠</span>' +
        '<span>Update ready — will apply when this app is stopped</span>';
      document.body.appendChild(banner);
    }
    banner.style.display = 'flex';
  }

  function check() {
    fetch(HUB + '/api/update-pending', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { if (j) ensure(!!j.pending); })
      .catch(function () { /* hub unreachable — ignore */ });
  }

  // first check once the page is ready; then every 15 s
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', check);
  } else {
    check();
  }
  setInterval(check, 15000);
})();
"""


@app.get("/")
def index():
    return PAGE_HTML


# ------------------------------ the webpage ---------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0e0b14">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>AdCreateHub Control</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>&#9889;</text></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@700;800&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@500&display=swap" rel="stylesheet">
<style>
  :root {
    color-scheme: dark;
    --bg:      #0e0b14;
    --card:    rgba(255,255,255,0.045);
    --edge:    rgba(255,255,255,0.09);
    --ink:     #efeaff;
    --muted:   #9a92b8;
    --violet:  #8b5cf6;
    --banana:  #fbbf24;
    --mint:    #34d399;
    --coral:   #f87171;
  }
  * { box-sizing: border-box; margin: 0; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body {
    display: flex; align-items: flex-start; justify-content: center;
    background: var(--bg); color: var(--ink);
    padding: calc(env(safe-area-inset-top, 0px) + 20px) 16px
             calc(env(safe-area-inset-bottom, 0px) + 20px);
    font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
    overflow-x: hidden; position: relative;
    min-height: 100%;
  }
  /* Aurora background */
  .orb { position: fixed; border-radius: 50%; filter: blur(90px); opacity: .32;
         pointer-events: none; z-index: 0; }
  .orb.a { width: 480px; height: 480px; background: var(--violet);
           top: -140px; left: -120px; animation: drifta 26s ease-in-out infinite alternate; }
  .orb.b { width: 420px; height: 420px; background: var(--banana); opacity: .14;
           bottom: -160px; right: -120px; animation: driftb 32s ease-in-out infinite alternate; }
  @keyframes drifta { to { transform: translate(70px, 60px); } }
  @keyframes driftb { to { transform: translate(-80px, -50px); } }

  /* The outer frame — one column on mobile, two cards on wider screens */
  .wrap { position: relative; z-index: 1; width: 100%; max-width: 820px; }
  .brand { text-align: center; margin: 6px 0 18px; }
  .brand .word {
    font-family: Outfit, Inter, sans-serif; font-weight: 800; font-size: 28px;
    letter-spacing: 1.5px;
    background: linear-gradient(92deg, #c4b5fd 0%, #f0abfc 55%, var(--banana) 115%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .brand .sub { font-size: 11px; font-weight: 600; letter-spacing: 3px;
                text-transform: uppercase; color: var(--muted); margin-top: 4px; }

  .grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
  @media (min-width: 720px) { .grid { grid-template-columns: 1fr 1fr; } }

  .card {
    text-align: center;
    background: var(--card); border: 1px solid var(--edge);
    border-radius: 24px; padding: 28px 20px 22px; backdrop-filter: blur(16px);
    box-shadow: 0 20px 54px rgba(0,0,0,.5);
  }
  .card h2 {
    font-family: Outfit, Inter, sans-serif; font-weight: 800;
    font-size: 20px; letter-spacing: .8px; margin-bottom: 2px;
  }
  .card .flag {
    font-size: 10px; font-weight: 600; letter-spacing: 2.4px;
    text-transform: uppercase; color: var(--muted); margin-bottom: 20px;
  }

  /* Power button: status lamp + control in one */
  .ringwrap { position: relative; width: 128px; height: 128px; margin: 0 auto 16px;
              border-radius: 50%; transition: box-shadow .6s ease; }
  .ringspin { position: absolute; inset: 0; border-radius: 50%; opacity: 0;
              background: conic-gradient(from 0deg, transparent 15%, var(--banana), var(--violet), transparent 85%);
              transition: opacity .4s ease; }
  .power {
    position: absolute; inset: 5px; border-radius: 50%; border: 2px solid var(--edge);
    background: #171321; cursor: pointer; display: flex; align-items: center;
    justify-content: center; transition: border-color .4s, transform .08s;
    padding: 0;
  }
  .power:active:not(:disabled) { transform: scale(.97); }
  .power:disabled { cursor: default; }
  .power:focus-visible { outline: 3px solid var(--violet); outline-offset: 3px; }
  .power svg { width: 46px; height: 46px; stroke: var(--muted); fill: none;
               stroke-width: 7; stroke-linecap: round; transition: stroke .4s, filter .4s; }

  .card.st-starting .ringspin, .card.st-stopping .ringspin { opacity: 1; animation: spin 1.5s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .card.st-online .ringwrap { animation: breathe 3s ease-in-out infinite; }
  @keyframes breathe {
    0%, 100% { box-shadow: 0 0 32px rgba(52,211,153,.35); }
    50%      { box-shadow: 0 0 56px rgba(52,211,153,.6); }
  }
  .card.st-online .power { border-color: var(--mint); }
  .card.st-online .power svg { stroke: var(--mint); filter: drop-shadow(0 0 8px rgba(52,211,153,.8)); }
  .card.st-error .power { border-color: var(--coral); }
  .card.st-error .power svg { stroke: var(--coral); }
  .card.st-offline .power:hover svg { stroke: var(--ink); }

  .statusword { font-family: Outfit, Inter, sans-serif; font-weight: 700;
                font-size: 22px; letter-spacing: .4px; margin-bottom: 6px; }
  .card.st-online  .statusword { color: var(--mint); }
  .card.st-error   .statusword { color: var(--coral); }
  .card.st-starting .statusword, .card.st-stopping .statusword { color: var(--banana); }
  .hint { font-size: 13px; color: var(--muted); line-height: 1.55; min-height: 20px; padding: 0 6px; }

  .confirm { display: none; gap: 10px; margin-top: 14px; }
  .confirm.show { display: flex; }
  .confirm button { flex: 1; font: inherit; font-weight: 600; font-size: 14px;
    padding: 12px 0; border-radius: 12px; border: 1px solid var(--edge);
    cursor: pointer; background: rgba(255,255,255,.05); color: var(--ink); }
  .confirm .yes { background: linear-gradient(135deg, #ef4444, #dc2626);
                  border: none; color: #fff; }
  .confirm button:hover { filter: brightness(1.15); }

  .linkcard { display: none; margin-top: 18px; text-align: left;
    background: rgba(139,92,246,.10); border: 1px solid rgba(139,92,246,.35);
    border-radius: 14px; padding: 12px 14px; }
  .linkcard.show { display: block; animation: rise .35s ease; }
  @keyframes rise { from { opacity: 0; transform: translateY(8px); } }
  .linkbtns { display: flex; gap: 10px; }
  .linkbtns a, .linkbtns button {
    flex: 1; font: inherit; font-weight: 600; font-size: 14px; text-align: center;
    padding: 12px 0; border-radius: 11px; cursor: pointer; text-decoration: none;
    transition: filter .15s;
  }
  .linkbtns a { background: linear-gradient(135deg, #8b5cf6, #7c3aed); color: #fff; border: none; }
  .linkbtns button { background: rgba(255,255,255,.06); color: var(--ink);
                     border: 1px solid var(--edge); }
  .linkbtns a:hover, .linkbtns button:hover { filter: brightness(1.14); }

  .errcard { display: none; margin-top: 16px; text-align: left; font-size: 13px;
    color: #fecaca; line-height: 1.5; background: rgba(248,113,113,.08);
    border: 1px solid rgba(248,113,113,.3); border-radius: 12px; padding: 12px 14px; }
  .errcard.show { display: block; }
  .errcard pre { margin-top: 9px; font-family: "JetBrains Mono", monospace; font-size: 11px;
    color: #cfc8e8; white-space: pre-wrap; word-break: break-all;
    max-height: 150px; overflow: auto; }

  /* Password gate */
  .gate {
    max-width: 400px; margin: 20vh auto 0; text-align: center;
    background: var(--card); border: 1px solid var(--edge);
    border-radius: 24px; padding: 32px 22px 24px; backdrop-filter: blur(16px);
    box-shadow: 0 20px 54px rgba(0,0,0,.5);
  }
  .gatetext { font-size: 13.5px; color: var(--muted); margin: 4px 0 18px; }
  .gate input {
    font: inherit; font-size: 16px; width: 100%; padding: 14px 16px; text-align: center;
    border-radius: 13px; border: 1px solid var(--edge);
    background: rgba(0,0,0,.3); color: var(--ink); letter-spacing: 2px;
  }
  .gate input:focus { outline: 2px solid var(--violet); border-color: transparent; }
  .gate .unlock { font: inherit; font-weight: 700; font-size: 16px; width: 100%;
    margin-top: 13px; padding: 14px 0; border-radius: 13px; border: none; cursor: pointer;
    background: linear-gradient(135deg, #8b5cf6, #7c3aed); color: #fff; }
  .gate .unlock:hover { filter: brightness(1.12); }
  .gateerr { font-size: 13px; color: var(--coral); margin-top: 12px; min-height: 18px; }
  .shake { animation: shake .4s; }
  @keyframes shake {
    20% { transform: translateX(-8px); } 40% { transform: translateX(8px); }
    60% { transform: translateX(-5px); } 80% { transform: translateX(5px); }
  }

  .foot { margin-top: 18px; font-size: 11px; color: #6e678c; text-align: center; }

  @media (prefers-reduced-motion: reduce) {
    .orb, .ringspin, .card.st-online .ringwrap { animation: none !important; }
    .card.st-online .ringwrap { box-shadow: 0 0 44px rgba(52,211,153,.45); }
  }
</style>
</head>
<body>
<div class="orb a"></div><div class="orb b"></div>

<div class="gate" id="gate" style="display:none">
  <div class="brand" style="margin-bottom: 14px;">
    <div class="word">ADCREATEHUB</div>
    <div class="sub">Remote Control</div>
  </div>
  <div class="gatetext">&#128274; Enter your password to open the panel.</div>
  <input id="pw" type="password" placeholder="Password" autocomplete="current-password" inputmode="text"
         onkeydown="if(event.key==='Enter')unlock()">
  <button class="unlock" onclick="unlock()">Unlock</button>
  <div class="gateerr" id="gateerr"></div>
</div>

<div class="wrap" id="panel" style="display:none">
  <div class="brand">
    <div class="word">ADCREATEHUB</div>
    <div class="sub">Remote Control</div>
  </div>
  <div class="grid" id="cards"></div>
  <div class="foot">Anyone with an app's link can generate with your credits — share wisely.</div>
</div>

<template id="card-tpl">
  <div class="card st-offline" data-key="">
    <h2 data-el="name"></h2>
    <div class="flag" data-el="port"></div>
    <div class="ringwrap">
      <div class="ringspin"></div>
      <button class="power" data-el="power" aria-label="Start or stop">
        <svg viewBox="0 0 100 100" aria-hidden="true">
          <path d="M50 14 V48"/>
          <path d="M30.5 27 A34 34 0 1 0 69.5 27"/>
        </svg>
      </button>
    </div>
    <div class="statusword" data-el="statusword">Checking&#8230;</div>
    <div class="hint" data-el="hint"></div>
    <div class="confirm" data-el="confirm">
      <button class="yes" data-el="stopyes">Stop</button>
      <button data-el="stopno">Keep running</button>
    </div>
    <div class="linkcard" data-el="linkcard">
      <div class="linkbtns">
        <a data-el="openlink" href="#" target="_blank" rel="noopener">Open</a>
        <button data-el="copybtn">Copy link</button>
      </div>
    </div>
    <div class="errcard" data-el="errcard">
      <span data-el="errtext"></span>
      <pre data-el="errlog"></pre>
    </div>
  </div>
</template>

<script>
var KEY = new URLSearchParams(location.search).get("key") || localStorage.getItem("adch_key") || "";
var unlocked = false, busy = {}, confirming = {}, cards = {};

function el(id){ return document.getElementById(id); }

function appUrl(port){ return "http://" + location.hostname + ":" + port + "/"; }

function showGate(msg){
  unlocked = false;
  el("panel").style.display = "none";
  el("gate").style.display = "block";
  el("gateerr").textContent = msg || "";
  if (msg) {
    el("gate").classList.remove("shake");
    void el("gate").offsetWidth;
    el("gate").classList.add("shake");
  }
  el("pw").focus();
}

async function apiGet(path){
  var r = await fetch(path + "?key=" + encodeURIComponent(KEY));
  if (r.status === 401) throw new Error("401");
  return r.json();
}
async function apiPost(path){
  var r = await fetch(path + "?key=" + encodeURIComponent(KEY), { method: "POST" });
  if (r.status === 401) throw new Error("401");
  return r.json();
}

async function unlock(){
  KEY = el("pw").value.trim();
  el("pw").value = "";
  try {
    var s = await apiGet("/api/status");
    localStorage.setItem("adch_key", KEY);
    unlocked = true;
    el("gate").style.display = "none";
    el("panel").style.display = "block";
    buildCards(s.apps);
    renderAll(s);
  } catch(e) {
    if (e.message === "401") showGate("That's not it — try again.");
    else showGate("Can't reach the server right now.");
  }
}

function fmtUptime(sec){
  var m = Math.floor(sec / 60);
  if (m < 1) return "just now";
  if (m < 60) return m + " min";
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}

function buildCards(apps){
  var box = el("cards");
  if (box.dataset.built === "1") return;
  box.dataset.built = "1";
  box.innerHTML = "";
  var tpl = el("card-tpl");
  apps.forEach(function(a){
    var node = tpl.content.cloneNode(true);
    var card = node.querySelector(".card");
    card.dataset.key = a.key;
    card.querySelector('[data-el="name"]').textContent = a.name;
    card.querySelector('[data-el="port"]').textContent = "Port " + a.port;
    var copyBtn = card.querySelector('[data-el="copybtn"]');
    copyBtn.addEventListener("click", function(){ copyLink(a.port, copyBtn); });
    var power = card.querySelector('[data-el="power"]');
    power.addEventListener("click", function(){ powerPressed(a.key); });
    var stopYes = card.querySelector('[data-el="stopyes"]');
    stopYes.addEventListener("click", function(){ confirmStop(a.key, true); });
    stopYes.textContent = "Stop " + a.name;
    var stopNo = card.querySelector('[data-el="stopno"]');
    stopNo.addEventListener("click", function(){ confirmStop(a.key, false); });
    box.appendChild(node);
    cards[a.key] = box.querySelector('.card[data-key="' + a.key + '"]');
  });
}

function renderAll(s){
  s.apps.forEach(renderOne);
}

function renderOne(a){
  var card = cards[a.key];
  if (!card) return;
  card.className = "card st-" + a.state;
  var q = function(sel){ return card.querySelector('[data-el="' + sel + '"]'); };
  var word = q("statusword"), hint = q("hint"), power = q("power");
  q("linkcard").classList.remove("show");
  q("errcard").classList.remove("show");
  if (a.state !== "online") { confirming[a.key] = false; q("confirm").classList.remove("show"); }
  power.disabled = !!busy[a.key];

  if (a.state === "offline") {
    word.textContent = "Offline";
    hint.textContent = "Tap the power button to start " + a.name + ".";
  } else if (a.state === "starting") {
    word.textContent = "Waking up…";
    hint.textContent = "Starting " + a.name + ". This page updates by itself — first start can take a couple of minutes.";
    power.disabled = true;
  } else if (a.state === "online") {
    word.textContent = "Online";
    hint.textContent = confirming[a.key] ? "Stop " + a.name + "? Anything mid-generation will be lost."
                                         : "Running for " + fmtUptime(a.uptime) + ".";
    q("confirm").classList.toggle("show", !!confirming[a.key]);
    q("openlink").href = appUrl(a.port);
    q("openlink").textContent = "Open " + a.name;
    q("linkcard").classList.add("show");
  } else if (a.state === "stopping") {
    word.textContent = "Stopping…";
    hint.textContent = "Shutting " + a.name + " down cleanly.";
    power.disabled = true;
  } else if (a.state === "error") {
    word.textContent = "Problem";
    hint.textContent = "Tap the power button to try again.";
    q("errtext").textContent = a.error || "Something went wrong.";
    q("errlog").textContent = (a.log || []).join("\n");
    q("errcard").classList.add("show");
  }
}

async function refresh(){
  if (!unlocked) return;
  try {
    var s = await apiGet("/api/status");
    buildCards(s.apps);
    renderAll(s);
  }
  catch(e){
    if (e.message === "401") { localStorage.removeItem("adch_key"); showGate("Session ended — enter your password."); }
    else { Object.keys(cards).forEach(function(k){
      cards[k].querySelector('[data-el="statusword"]').textContent = "No connection";
      cards[k].querySelector('[data-el="hint"]').textContent = "Can't reach the server — retrying…";
    }); }
  }
}

async function powerPressed(key){
  if (busy[key]) return;
  var card = cards[key];
  var state = card.className.replace("card st-", "").split(" ")[0];
  if (state === "online") { confirming[key] = !confirming[key]; refresh(); return; }
  if (state === "offline" || state === "error") {
    busy[key] = true;
    card.querySelector('[data-el="power"]').disabled = true;
    try { await apiPost("/api/" + encodeURIComponent(key) + "/start"); } catch(e){}
    busy[key] = false;
    refresh();
  }
}

async function confirmStop(key, yes){
  confirming[key] = false;
  if (yes) { try { await apiPost("/api/" + encodeURIComponent(key) + "/stop"); } catch(e){} }
  refresh();
}

function copyLink(port, btn){
  var text = appUrl(port);
  function flash(msg){
    btn.textContent = msg;
    setTimeout(function(){ btn.textContent = "Copy link"; }, 1400);
  }
  function legacyCopy(){
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed"; ta.style.top = "0"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus(); ta.select(); ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch(e){}
    document.body.removeChild(ta);
    flash(ok ? "Copied!" : "Couldn't copy");
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text)
      .then(function(){ flash("Copied!"); }, legacyCopy);
  } else {
    legacyCopy();
  }
}

(async function boot(){
  if (!KEY) { showGate(""); return; }
  try {
    var s = await apiGet("/api/status");
    unlocked = true;
    el("gate").style.display = "none";
    el("panel").style.display = "block";
    buildCards(s.apps);
    renderAll(s);
  } catch(e) {
    localStorage.removeItem("adch_key");
    showGate("");
  }
})();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print(f"AdCreateHub control panel running on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
