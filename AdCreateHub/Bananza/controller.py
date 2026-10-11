"""
Bananza Remote Control Panel
============================
Runs 24/7 on your Oracle Cloud server. Gives you a password-protected webpage
with a big power button that starts/stops bulk_banana_gui.py. The app is served
directly from this server on APP_PORT (no gradio --share tunnel), and the page
shows "Online" only once the app actually answers on that port.

NOTE: port APP_PORT (7860) must be open in your Oracle Cloud security list AND
the server firewall, just like port 8000 already is.

Your password lives in SECRET_KEY just below — change it any time, then run:
    sudo systemctl restart bananza-panel
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
SECRET_KEY = "minion01"                # <-- your password for the control page
PORT = 8000                            # the port the control page lives on
APP_PORT = 7860                        # the port Bananza itself is served on
APP_FILE = "bulk_banana_gui.py"        # the app this panel starts/stops
START_TIMEOUT_SECONDS = 240            # give up if app isn't up after 4 min
# ----------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__)

# ---- shared state (guarded by _lock) ----
_lock = threading.Lock()
_state = "offline"          # offline | starting | online | stopping | error
_error_msg = None
_proc = None
_started_at = None
_log = deque(maxlen=300)    # last lines the app printed (for troubleshooting)


def _set(state=None, err="__keep__"):
    global _state, _error_msg
    with _lock:
        if state is not None:
            _state = state
        if err != "__keep__":
            _error_msg = err


def _watch_output(proc):
    """Read the app's printed output line by line (kept for troubleshooting)."""
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\r\n")
            if line.strip():
                _log.append(line)
    except Exception as e:
        _log.append(f"[controller] output reader stopped: {e}")
    # The loop above ends when the app's process exits.
    proc.wait()
    with _lock:
        was = _state
    if was == "starting":
        _set(state="error",
             err="The app closed while starting up. The last log lines below "
                 "usually say why.")
    elif was in ("online", "stopping"):
        _set(state="offline", err=None)


def _watch_ready(proc):
    """Poll the app's port until it answers — that's when we call it online."""
    global _started_at
    while True:
        time.sleep(1.5)
        with _lock:
            if _proc is not proc or _state != "starting":
                return
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{APP_PORT}/", timeout=2)
        except urllib.error.HTTPError:
            pass                       # any HTTP answer means the server is up
        except Exception:
            continue                   # not listening yet — keep waiting
        _set(state="online", err=None)
        _started_at = time.time()
        return


def _watch_timeout(proc):
    """If the app never comes up, kill it so we don't hang forever."""
    waited = 0
    while waited < START_TIMEOUT_SECONDS:
        time.sleep(2)
        waited += 2
        with _lock:
            if _state != "starting" or _proc is not proc:
                return
    _kill(proc)
    _set(state="error",
         err=f"The app didn't come online after {START_TIMEOUT_SECONDS} seconds "
             "— gave up. Press the button to try again.")


def _kill(proc):
    """Stop the app AND every helper it spawned (like the share tunnel)."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(20):                       # up to 10s of patience
        if proc.poll() is not None:
            return
        time.sleep(0.5)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _do_start():
    global _proc
    with _lock:
        if _state in ("starting", "online", "stopping"):
            return False
    _log.clear()
    _set(state="starting", err=None)
    env = {
        **os.environ,
        "BANANA_HOST": "0.0.0.0",       # serve to the outside world directly
        "BANANA_PORT": str(APP_PORT),   # on this fixed port
        "BANANA_NO_BROWSER": "1",       # servers have no browser to open
        "PYTHONUNBUFFERED": "1",        # see the app's printouts instantly
    }
    proc = subprocess.Popen(
        [sys.executable, "-u", str(BASE_DIR / APP_FILE)],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        start_new_session=True,     # lets us stop the whole family later
    )
    with _lock:
        _proc = proc
    threading.Thread(target=_watch_output, args=(proc,), daemon=True).start()
    threading.Thread(target=_watch_ready, args=(proc,), daemon=True).start()
    threading.Thread(target=_watch_timeout, args=(proc,), daemon=True).start()
    return True


def _do_stop():
    with _lock:
        proc = _proc
        stoppable = proc is not None and _state in ("starting", "online")
    if not stoppable:
        return False
    _set(state="stopping")
    threading.Thread(target=_kill, args=(proc,), daemon=True).start()
    return True


# ----------------------------- web endpoints --------------------------------

def _authorized():
    supplied = request.args.get("key") or request.headers.get("X-Key") or ""
    return supplied == SECRET_KEY


@app.get("/api/status")
def api_status():
    if not _authorized():
        return jsonify({"error": "bad key"}), 401
    with _lock:
        state, err = _state, _error_msg
        up = int(time.time() - _started_at) if (_started_at and state == "online") else 0
    tail = list(_log)[-15:] if state == "error" else []
    return jsonify({"state": state, "port": APP_PORT, "error": err,
                    "uptime": up, "log": tail})


@app.post("/api/start")
def api_start():
    if not _authorized():
        return jsonify({"error": "bad key"}), 401
    _do_start()
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    if not _authorized():
        return jsonify({"error": "bad key"}), 401
    _do_stop()
    return jsonify({"ok": True})


@app.get("/")
def index():
    return PAGE_HTML


# ------------------------------ the webpage ---------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bananza Control</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>&#127820;</text></svg>">
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
  * { box-sizing: border-box; margin: 0; }
  html, body { height: 100%; }
  body {
    display: flex; align-items: center; justify-content: center;
    background: var(--bg); color: var(--ink); padding: 20px;
    font-family: Inter, system-ui, -apple-system, "Segoe UI", sans-serif;
    overflow: hidden; position: relative;
  }
  /* ---- slow aurora background ---- */
  .orb { position: fixed; border-radius: 50%; filter: blur(90px); opacity: .32;
         pointer-events: none; z-index: 0; }
  .orb.a { width: 480px; height: 480px; background: var(--violet);
           top: -140px; left: -120px; animation: drifta 26s ease-in-out infinite alternate; }
  .orb.b { width: 420px; height: 420px; background: var(--banana); opacity: .14;
           bottom: -160px; right: -120px; animation: driftb 32s ease-in-out infinite alternate; }
  @keyframes drifta { to { transform: translate(70px, 60px); } }
  @keyframes driftb { to { transform: translate(-80px, -50px); } }

  .card {
    position: relative; z-index: 1; width: 100%; max-width: 400px; text-align: center;
    background: var(--card); border: 1px solid var(--edge);
    border-radius: 26px; padding: 36px 26px 28px; backdrop-filter: blur(16px);
    box-shadow: 0 24px 70px rgba(0,0,0,.5);
  }
  .wordmark {
    font-family: Outfit, Inter, sans-serif; font-weight: 800; font-size: 26px;
    letter-spacing: 1.5px;
    background: linear-gradient(92deg, #c4b5fd 0%, #f0abfc 55%, var(--banana) 115%);
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }
  .tagline { font-size: 11.5px; font-weight: 600; letter-spacing: 3px;
             text-transform: uppercase; color: var(--muted); margin: 4px 0 30px; }

  /* ---- the power button: status lamp + control in one ---- */
  .ringwrap { position: relative; width: 148px; height: 148px; margin: 0 auto 20px;
              border-radius: 50%; transition: box-shadow .6s ease; }
  .ringspin { position: absolute; inset: 0; border-radius: 50%; opacity: 0;
              background: conic-gradient(from 0deg, transparent 15%, var(--banana), var(--violet), transparent 85%);
              transition: opacity .4s ease; }
  .power {
    position: absolute; inset: 5px; border-radius: 50%; border: 2px solid var(--edge);
    background: #171321; cursor: pointer; display: flex; align-items: center;
    justify-content: center; transition: border-color .4s, transform .08s;
    -webkit-tap-highlight-color: transparent;
  }
  .power:active:not(:disabled) { transform: scale(.97); }
  .power:disabled { cursor: default; }
  .power:focus-visible { outline: 3px solid var(--violet); outline-offset: 3px; }
  .power svg { width: 52px; height: 52px; stroke: var(--muted); fill: none;
               stroke-width: 7; stroke-linecap: round; transition: stroke .4s, filter .4s; }

  .st-starting .ringspin, .st-stopping .ringspin { opacity: 1; animation: spin 1.5s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .st-online .ringwrap { animation: breathe 3s ease-in-out infinite; }
  @keyframes breathe {
    0%, 100% { box-shadow: 0 0 34px rgba(52,211,153,.35); }
    50%      { box-shadow: 0 0 60px rgba(52,211,153,.6); }
  }
  .st-online .power { border-color: var(--mint); }
  .st-online .power svg { stroke: var(--mint); filter: drop-shadow(0 0 8px rgba(52,211,153,.8)); }
  .st-error .power { border-color: var(--coral); }
  .st-error .power svg { stroke: var(--coral); }
  .st-offline .power:hover svg { stroke: var(--ink); }

  .statusword { font-family: Outfit, Inter, sans-serif; font-weight: 700;
                font-size: 24px; letter-spacing: .4px; margin-bottom: 6px; }
  .st-online  .statusword { color: var(--mint); }
  .st-error   .statusword { color: var(--coral); }
  .st-starting .statusword, .st-stopping .statusword { color: var(--banana); }
  .hint { font-size: 13px; color: var(--muted); line-height: 1.55; min-height: 20px; }

  /* ---- confirm-stop row ---- */
  .confirm { display: none; gap: 10px; margin-top: 14px; }
  .confirm.show { display: flex; }
  .confirm button { flex: 1; font: inherit; font-weight: 600; font-size: 14px;
    padding: 11px 0; border-radius: 12px; border: 1px solid var(--edge);
    cursor: pointer; background: rgba(255,255,255,.05); color: var(--ink); }
  .confirm .yes { background: linear-gradient(135deg, #ef4444, #dc2626);
                  border: none; color: #fff; }
  .confirm button:hover { filter: brightness(1.15); }

  /* ---- link card ---- */
  .linkcard { display: none; margin-top: 22px; text-align: left;
    background: rgba(139,92,246,.10); border: 1px solid rgba(139,92,246,.35);
    border-radius: 16px; padding: 14px 16px; }
  .linkcard.show { display: block; animation: rise .35s ease; }
  @keyframes rise { from { opacity: 0; transform: translateY(8px); } }
  .linkbtns { display: flex; gap: 10px; }
  .linkbtns a, .linkbtns button {
    flex: 1; font: inherit; font-weight: 600; font-size: 14px; text-align: center;
    padding: 11px 0; border-radius: 12px; cursor: pointer; text-decoration: none;
    transition: filter .15s;
  }
  .linkbtns a { background: linear-gradient(135deg, #8b5cf6, #7c3aed); color: #fff; border: none; }
  .linkbtns button { background: rgba(255,255,255,.06); color: var(--ink);
                     border: 1px solid var(--edge); }
  .linkbtns a:hover, .linkbtns button:hover { filter: brightness(1.14); }

  /* ---- error card ---- */
  .errcard { display: none; margin-top: 20px; text-align: left; font-size: 13px;
    color: #fecaca; line-height: 1.5; background: rgba(248,113,113,.08);
    border: 1px solid rgba(248,113,113,.3); border-radius: 14px; padding: 13px 15px; }
  .errcard.show { display: block; }
  .errcard pre { margin-top: 9px; font-family: "JetBrains Mono", monospace; font-size: 11px;
    color: #cfc8e8; white-space: pre-wrap; word-break: break-all;
    max-height: 170px; overflow: auto; }

  /* ---- password gate ---- */
  .gatetext { font-size: 13.5px; color: var(--muted); margin-bottom: 18px; }
  .gate input {
    font: inherit; font-size: 16px; width: 100%; padding: 13px 16px; text-align: center;
    border-radius: 13px; border: 1px solid var(--edge);
    background: rgba(0,0,0,.3); color: var(--ink); letter-spacing: 2px;
  }
  .gate input:focus { outline: 2px solid var(--violet); border-color: transparent; }
  .gate .unlock { font: inherit; font-weight: 700; font-size: 16px; width: 100%;
    margin-top: 13px; padding: 13px 0; border-radius: 13px; border: none; cursor: pointer;
    background: linear-gradient(135deg, #8b5cf6, #7c3aed); color: #fff; }
  .gate .unlock:hover { filter: brightness(1.12); }
  .gateerr { font-size: 13px; color: var(--coral); margin-top: 12px; min-height: 18px; }
  .shake { animation: shake .4s; }
  @keyframes shake {
    20% { transform: translateX(-8px); } 40% { transform: translateX(8px); }
    60% { transform: translateX(-5px); } 80% { transform: translateX(5px); }
  }

  .foot { margin-top: 24px; font-size: 11px; color: #6e678c; }

  @media (prefers-reduced-motion: reduce) {
    .orb, .ringspin, .st-online .ringwrap { animation: none !important; }
    .st-online .ringwrap { box-shadow: 0 0 44px rgba(52,211,153,.45); }
  }
</style>
</head>
<body>
<div class="orb a"></div><div class="orb b"></div>

<div class="card gate" id="gate" style="display:none">
  <div class="wordmark">BANANZA</div>
  <div class="tagline">Remote Control</div>
  <div class="gatetext">&#128274; Enter your password to open the panel.</div>
  <input id="pw" type="password" placeholder="Password" autocomplete="current-password"
         onkeydown="if(event.key==='Enter')unlock()">
  <button class="unlock" onclick="unlock()">Unlock</button>
  <div class="gateerr" id="gateerr"></div>
</div>

<div class="card st-offline" id="panel" style="display:none">
  <div class="wordmark">BANANZA</div>
  <div class="tagline">Remote Control</div>

  <div class="ringwrap">
    <div class="ringspin"></div>
    <button class="power" id="powerbtn" onclick="powerPressed()" aria-label="Start or stop Bananza">
      <svg viewBox="0 0 100 100" aria-hidden="true">
        <path d="M50 14 V48"/>
        <path d="M30.5 27 A34 34 0 1 0 69.5 27"/>
      </svg>
    </button>
  </div>

  <div class="statusword" id="statusword">Checking&#8230;</div>
  <div class="hint" id="hint"></div>

  <div class="confirm" id="confirm">
    <button class="yes" onclick="confirmStop(true)">Stop Bananza</button>
    <button onclick="confirmStop(false)">Keep running</button>
  </div>

  <div class="linkcard" id="linkcard">
    <div class="linkbtns">
      <a id="openlink" href="#">Open Bananza</a>
      <button id="copybtn" onclick="copyLink(this)">Copy link</button>
    </div>
  </div>

  <div class="errcard" id="errcard"><span id="errtext"></span><pre id="errlog"></pre></div>
  <div class="foot" id="foot"></div>
</div>

<script>
var KEY = new URLSearchParams(location.search).get("key") || localStorage.getItem("bananza_key") || "";
var unlocked = false, state = "unknown", busy = false, confirming = false;
var appPort = 7860;

function el(id){ return document.getElementById(id); }

/* Bananza runs on this same server, just on another port — so build its
   address from whatever hostname the browser used to reach this panel. */
function appUrl(){ return "http://" + location.hostname + ":" + appPort + "/"; }

function showGate(msg){
  unlocked = false;
  el("panel").style.display = "none";
  el("gate").style.display = "block";
  el("gateerr").textContent = msg || "";
  if (msg) {
    el("gate").classList.remove("shake");
    void el("gate").offsetWidth;           /* restart the animation */
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
    localStorage.setItem("bananza_key", KEY);
    unlocked = true;
    el("gate").style.display = "none";
    el("panel").style.display = "block";
    render(s);
  } catch(e) {
    if (e.message === "401") showGate("That's not it &#8212; try again.");
    else showGate("Can't reach the server right now.");
  }
}

function fmtUptime(sec){
  var m = Math.floor(sec / 60);
  if (m < 1) return "just now";
  if (m < 60) return m + " min";
  return Math.floor(m / 60) + "h " + (m % 60) + "m";
}

function render(s){
  state = s.state;
  var p = el("panel");
  p.className = "card st-" + state;
  var word = el("statusword"), hint = el("hint"), btn = el("powerbtn");
  el("linkcard").classList.remove("show");
  el("errcard").classList.remove("show");
  if (state !== "online") { confirming = false; el("confirm").classList.remove("show"); }
  btn.disabled = busy;
  el("foot").textContent = "";

  if (state === "offline") {
    word.textContent = "Offline";
    hint.textContent = "Tap the power button to start.";
  } else if (state === "starting") {
    word.textContent = "Waking up\u2026";
    hint.textContent = "Starting Bananza. This page updates by itself \u2014 first start can take a couple of minutes.";
    btn.disabled = true;
  } else if (state === "online") {
    word.textContent = "Online";
    hint.textContent = confirming ? "Stop Bananza? Anything mid-generation will be lost."
                                  : "Running for " + fmtUptime(s.uptime) + ".";
    el("confirm").classList.toggle("show", confirming);
    if (s.port) appPort = s.port;
    el("openlink").href = appUrl();
    el("linkcard").classList.add("show");
    el("foot").textContent = "Anyone with the link can generate with your credits \u2014 share it wisely.";
  } else if (state === "stopping") {
    word.textContent = "Stopping\u2026";
    hint.textContent = "Shutting Bananza down cleanly.";
    btn.disabled = true;
  } else if (state === "error") {
    word.textContent = "Problem";
    hint.textContent = "Tap the power button to try again.";
    el("errtext").textContent = s.error || "Something went wrong.";
    el("errlog").textContent = (s.log || []).join("\n");
    el("errcard").classList.add("show");
  }
}

async function refresh(){
  if (!unlocked) return;
  try { render(await apiGet("/api/status")); }
  catch(e){
    if (e.message === "401") { localStorage.removeItem("bananza_key"); showGate("Session ended \u2014 enter your password."); }
    else { el("statusword").textContent = "No connection"; el("hint").textContent = "Can't reach the server \u2014 retrying\u2026"; }
  }
}

async function powerPressed(){
  if (busy) return;
  if (state === "online") { confirming = !confirming; refresh(); return; }
  if (state === "offline" || state === "error") {
    busy = true; el("powerbtn").disabled = true;
    try { await apiPost("/api/start"); } catch(e){}
    busy = false; refresh();
  }
}

async function confirmStop(yes){
  confirming = false;
  if (yes) { try { await apiPost("/api/stop"); } catch(e){} }
  refresh();
}

function copyLink(btn){
  var text = appUrl();
  function flash(msg){
    btn.textContent = msg;
    setTimeout(function(){ btn.textContent = "Copy link"; }, 1400);
  }
  /* Old-school fallback: the modern clipboard API only exists on HTTPS
     pages, and this panel is served over plain HTTP. */
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
    render(s);
  } catch(e) {
    localStorage.removeItem("bananza_key");
    showGate("");
  }
})();
setInterval(refresh, 2500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print(f"Bananza control panel running on port {PORT}")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
