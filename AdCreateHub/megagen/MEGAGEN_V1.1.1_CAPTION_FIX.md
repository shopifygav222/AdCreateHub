# MEGAGEN v1.1.1 — Caption overlap fix (one file, three CSS edits)

Post-build patch for an already-implemented v1.1. Do NOT re-apply anything from
MEGAGEN_V1.1_CHANGES.md — only the three edits below, all in `static/style.css`.

## Root cause (already diagnosed — do not re-investigate)
Lightbox media is capped at `88vh` and vertically centered, so a tall video
leaves only a ~6vh (~65px @1080p) gap below it. v1.1 §C8 moved the video
caption to `bottom: 68px` to "clear the control bar" — but the control bar
lives INSIDE the player at the video's bottom edge and never needed clearing.
The 68px override pushes the caption (which is ~36px tall, so it spans
68–104px from the viewport bottom) straight onto the video and across the
seek bar. Fix = put the caption back at the shared `bottom: 16px`, and cap
media height at `min(88vh, calc(100vh - 120px))` so a ≥60px caption lane
always exists below centered media on ANY screen size — for images too.

## Edits — `static/style.css` only

Anchors below are the lines v1.1 produced. If your build's whitespace differs
slightly, apply the same property change to the matching selector.

**1.** OLD:
```css
.lb-img, .lb-vid { max-width: 96vw; max-height: 88vh; display: block; }
```
NEW:
```css
.lb-img, .lb-vid { max-width: 96vw; max-height: min(88vh, calc(100vh - 120px)); display: block; }
```

**2.** OLD:
```css
.lb-player video { max-width: 96vw; max-height: 88vh; display: block; background: #000; border-radius: 10px; }
```
NEW:
```css
.lb-player video { max-width: 96vw; max-height: min(88vh, calc(100vh - 120px)); display: block; background: #000; border-radius: 10px; }
```

**3.** DELETE this line entirely (in the §C8 block near the end of the file):
```css
#lightbox.vid .lb-caption { bottom: 68px; }
```

Nothing else changes. Do not touch `.lb-player:fullscreen video` (its
`100vh` override is more specific and still wins in fullscreen, where the
caption isn't shown anyway). No HTML or JS edits.

## Definition of done
1. `grep -c "bottom: 68px" static/style.css` → 0.
2. `grep -c "calc(100vh - 120px)" static/style.css` → 2.
3. CSS braces still balanced (same count of `{` and `}` as before minus one pair).
4. Visual: open a TALL (portrait) video fullscreen — the prompt caption sits fully BELOW the video, never over it or over the seek bar. Same check with a tall image, and once with the browser window shrunk to ~700px tall.
