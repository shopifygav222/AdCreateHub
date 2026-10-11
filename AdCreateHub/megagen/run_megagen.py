"""MegaGen launcher — branded console, uvicorn, auto-opens the browser."""
import sys

for _stream in (sys.stdout, sys.stderr):
    # Windows consoles still default to cp1252, which can't encode the banner —
    # switch to UTF-8 and never let a print() crash the launcher.
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

print("MEGAGEN is waking up...", flush=True)   # before the heavy imports

import os          # noqa: E402
import threading   # noqa: E402
import time        # noqa: E402
import webbrowser  # noqa: E402
from pathlib import Path  # noqa: E402

HOST = os.environ.get("MEGAGEN_HOST", "127.0.0.1")
# 7861 so it never collides with Bananza's 7860
PORT = int(os.environ.get("MEGAGEN_PORT", "7861"))
APP_VERSION = "1.0"
BASE_DIR = Path(__file__).resolve().parent
OUT_DIR = BASE_DIR / "output"
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
                "MegaGen — keep this window open while using the app")
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
    title = "M E G A G E N"
    sub = f"MEGAGEN — mega bulk generator  ·  v{APP_VERSION}"
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


def _print_ready(c, url):
    print(f"  {c.G}{c.B}√  The app is running!{c.R}")
    print()
    print(f"     {c.D}Web app{c.R}    {c.B}{url}{c.R}")
    print(f"     {c.D}Output{c.R}     {OUT_DIR.resolve()}")
    print()
    print(f"  {c.Y}{c.B}■  KEEP THIS WINDOW OPEN{c.R}{c.Y} — it powers the app.{c.R}")
    print(f"     {c.D}Minimize it if it's in the way.{c.R}")
    print()
    print(f"     {c.D}Browser didn't open by itself? Copy the address above{c.R}")
    print(f"     {c.D}into Chrome or Edge.{c.R}")
    print(f"     {c.D}Finished for today? Just close this window.{c.R}")
    print()
    print(f"  {c.V}{'═' * (_BOX_W + 2)}{c.R}")
    print()


def main():
    no_browser = "--no-browser" in sys.argv or bool(os.environ.get("MEGAGEN_NO_BROWSER"))
    tty = bool(sys.stdout) and hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
    ansi_on = tty and _enable_ansi()
    c = _Colors(ansi_on)
    _set_console_title()
    if tty:
        os.system("cls" if os.name == "nt" else "clear")
    _print_banner(c, ansi_on)

    loader = _Loader(c, tty, "Starting the app — your browser will open by itself…")
    loader.start()
    try:
        import uvicorn
        from app.server import app
        from app.config import DEFAULT_PROJECT, ensure_dirs
        from app import projects
        ensure_dirs()
        if not [d for d in OUT_DIR.iterdir() if d.is_dir() and d.name != ".trash"]:
            projects.project_dir(DEFAULT_PROJECT)

        config = uvicorn.Config(app, host=HOST, port=PORT, log_level="warning",
                                access_log=False)
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        t0 = time.time()
        while not server.started and thread.is_alive() and time.time() - t0 < 30:
            time.sleep(0.05)
    finally:
        loader.stop()

    if not server.started:
        raise RuntimeError(f"The server didn't start — is something else using port {PORT}?")

    url = f"http://{HOST}:{PORT}"
    _print_ready(c, url)
    if not no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
        print("\n  Bye! The app has shut down — you can close this window.")
    except KeyboardInterrupt:
        print("\n  Bye! The app has shut down — you can close this window.")
    except Exception:
        import traceback
        traceback.print_exc()
        print()
        print("  Something went wrong while starting the app (details above).")
        print("  Take a photo/screenshot of this window if you need help.")
        try:
            if sys.stdout and hasattr(sys.stdout, "isatty") and sys.stdout.isatty():
                input("\n  Press Enter to close this window... ")
        except Exception:
            pass
