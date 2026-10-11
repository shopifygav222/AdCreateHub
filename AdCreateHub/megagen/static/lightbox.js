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
