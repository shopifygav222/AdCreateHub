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
