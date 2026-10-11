/* MegaGen mobile wiring: panel drawer, kebab menu, generate-bar toggle, touch
   gestures (swipe in lightbox / trash viewer, pinch-zoom for images, long-press
   to multi-select tiles). Desktop behaviour is untouched — this file only adds
   handlers on top of what app.js / grid.js / lightbox.js already wire. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const MOBILE_Q = '(max-width: 900px)';
  const isMobile = () => window.matchMedia(MOBILE_Q).matches;

  // ---------------- panel drawer ----------------
  const panel = $('panel');
  const backdrop = $('panel-backdrop');

  function openDrawer() {
    if (!isMobile()) return;
    panel.classList.add('drawer-open');
    panel.classList.remove('collapsed');
    backdrop.classList.remove('hidden');
    document.body.style.overflow = 'hidden';
  }
  function closeDrawer() {
    panel.classList.remove('drawer-open');
    backdrop.classList.add('hidden');
    document.body.style.overflow = '';
  }

  $('btn-panel-drawer').addEventListener('click', (e) => {
    e.stopPropagation();
    if (panel.classList.contains('drawer-open')) closeDrawer(); else openDrawer();
  });
  const btnClose = $('btn-panel-close');
  if (btnClose) btnClose.addEventListener('click', closeDrawer);
  backdrop.addEventListener('click', closeDrawer);

  // auto-close drawer when the viewport grows back to desktop
  window.addEventListener('resize', () => { if (!isMobile()) closeDrawer(); });

  // ---------------- topbar kebab menu ----------------
  const menu = $('topbar-menu');
  const btnMenu = $('btn-topbar-menu');

  function openMenu() {
    const r = btnMenu.getBoundingClientRect();
    menu.style.top = (r.bottom + 6) + 'px';
    menu.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
    menu.classList.remove('hidden');
  }
  function closeMenu() { menu.classList.add('hidden'); }

  btnMenu.addEventListener('click', (e) => {
    e.stopPropagation();
    if (menu.classList.contains('hidden')) openMenu(); else closeMenu();
  });
  window.addEventListener('resize', () => { if (!menu.classList.contains('hidden')) openMenu(); });
  document.addEventListener('click', (e) => {
    if (!menu.classList.contains('hidden')
        && !e.target.closest('#topbar-menu')
        && e.target !== btnMenu) closeMenu();
  });

  // mobile menu items proxy to the desktop ones so wiring stays in one place
  $('btn-guide-m').addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); $('btn-guide').click(); });
  $('btn-settings-m').addEventListener('click', (e) => { e.stopPropagation(); closeMenu(); $('btn-settings').click(); });
  $('btn-view-m').addEventListener('click', (e) => {
    // stopPropagation so the document-click in app.js doesn't close the view-pop
    // we just opened (its handler hides view-pop when the click is outside #view-wrap)
    e.stopPropagation();
    closeMenu();
    $('view-pop').classList.toggle('hidden');
  });

  // ---------------- generate-bar toggle ----------------
  const genbar = $('genbar');
  const btnGen = $('btn-genbar-toggle');
  btnGen.addEventListener('click', () => {
    genbar.classList.toggle('gen-expanded');
    btnGen.textContent = genbar.classList.contains('gen-expanded') ? '✕' : '⚙';
    syncGenbarHeight();
  });

  // Keep --genbar-h in sync so run cards and toasts never hide behind an
  // expanded generate bar on mobile.
  function syncGenbarHeight() {
    const h = genbar.getBoundingClientRect().height;
    document.documentElement.style.setProperty('--genbar-h', h + 'px');
  }
  if ('ResizeObserver' in window) {
    new ResizeObserver(syncGenbarHeight).observe(genbar);
  } else {
    window.addEventListener('resize', syncGenbarHeight);
  }
  // run once on load
  setTimeout(syncGenbarHeight, 60);

  // ---------------- lightbox: ⋯ overflow menu (mobile only) ----------------
  const lbMoreBtn = $('lb-more-btn');
  const lbMorePop = $('lb-more-pop');
  function placeLbMore() {
    const r = lbMoreBtn.getBoundingClientRect();
    lbMorePop.style.top = (r.bottom + 6) + 'px';
    lbMorePop.style.right = Math.max(8, window.innerWidth - r.right) + 'px';
  }
  function closeLbMore() { lbMorePop.classList.add('hidden'); }
  function toggleLbMore() {
    if (lbMorePop.classList.contains('hidden')) { placeLbMore(); lbMorePop.classList.remove('hidden'); }
    else closeLbMore();
  }
  lbMoreBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleLbMore(); });
  lbMorePop.addEventListener('click', (e) => {
    // let the lightbox's own [data-lb] handler process the action, then close
    const b = e.target.closest('[data-lb]');
    if (b) {
      e.stopPropagation();
      closeLbMore();
      lbMoreBtn.blur();
      // forward the action — Lightbox's internal dispatcher reads data-lb on
      // the real buttons under .lb-actions, so synthesize a click there
      const target = document.querySelector('.lb-actions [data-lb="' + b.dataset.lb + '"]');
      if (target) target.click();
    }
  });
  document.addEventListener('click', (e) => {
    if (!lbMorePop.classList.contains('hidden')
        && !e.target.closest('#lb-more-pop')
        && e.target !== lbMoreBtn) closeLbMore();
  });
  window.addEventListener('resize', () => { if (!lbMorePop.classList.contains('hidden')) placeLbMore(); });

  // ---------------- lightbox: touch navigation + pinch zoom ----------------
  const lbRoot = $('lightbox');
  const lbImg = lbRoot.querySelector('.lb-img');
  const lbPlayer = lbRoot.querySelector('.lb-player');

  // Wire the mobile ‹ › navigation buttons
  lbRoot.querySelectorAll('[data-lb-nav]').forEach((b) => {
    b.addEventListener('click', (e) => {
      e.stopPropagation();
      const d = Number(b.dataset.lbNav) || 1;
      stepLB(d);
    });
  });

  function stepLB(delta) {
    const name = window.Lightbox && Lightbox.current && Lightbox.current();
    if (!name || !window.Grid) return;
    const i = Grid.indexOf(name);
    if (i < 0) return;
    let j = i + delta;
    while (Grid.items[j] && Grid.items[j].ghost) j += delta;
    const next = Grid.items[j];
    if (next) Lightbox.open(next.name);
  }

  // Swipe + pinch state (images). Videos keep native touch for the player.
  const Z = { scale: 1, tx: 0, ty: 0 };
  let gesture = null;    // {kind: 'swipe'|'pinch'|'pan', ...}

  function distance(a, b) {
    const dx = a.clientX - b.clientX, dy = a.clientY - b.clientY;
    return Math.sqrt(dx * dx + dy * dy);
  }
  function midpoint(a, b) {
    return { x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 };
  }

  function applyImgTransform() {
    if (!lbImg) return;
    if (Z.scale <= 1.001 && Z.tx === 0 && Z.ty === 0) {
      lbImg.style.transform = '';
    } else {
      lbImg.style.transformOrigin = 'center center';
      lbImg.style.transform =
        'translate(' + Z.tx + 'px,' + Z.ty + 'px) scale(' + Z.scale + ')';
    }
  }

  function resetTouchZoom() { Z.scale = 1; Z.tx = 0; Z.ty = 0; applyImgTransform(); }

  function imgIsCurrent() {
    return !lbRoot.classList.contains('hidden')
      && !lbImg.classList.contains('hidden')
      && lbImg.getAttribute('src');
  }

  // reset zoom whenever the lightbox swaps media
  const lbObserver = new MutationObserver((muts) => {
    for (const m of muts) {
      if (m.type === 'attributes' && m.attributeName === 'src') resetTouchZoom();
      if (m.type === 'attributes' && m.attributeName === 'class') {
        if (lbRoot.classList.contains('hidden')) resetTouchZoom();
      }
    }
  });
  lbObserver.observe(lbImg, { attributes: true, attributeFilter: ['src'] });
  lbObserver.observe(lbRoot, { attributes: true, attributeFilter: ['class'] });

  const lbStage = lbRoot.querySelector('.lb-stage');

  lbStage.addEventListener('touchstart', (e) => {
    if (lbRoot.classList.contains('hidden')) return;
    if (e.target.closest('.lb-actions, .lb-caption, .lb-nav, .lb-player')) return;
    const touches = e.touches;
    if (touches.length === 2 && imgIsCurrent()) {
      e.preventDefault();
      gesture = {
        kind: 'pinch',
        d0: distance(touches[0], touches[1]),
        mid: midpoint(touches[0], touches[1]),
        s0: Z.scale,
        tx0: Z.tx, ty0: Z.ty,
      };
    } else if (touches.length === 1) {
      const t = touches[0];
      if (Z.scale > 1 && imgIsCurrent()) {
        // pan while zoomed
        gesture = { kind: 'pan', x0: t.clientX, y0: t.clientY, tx0: Z.tx, ty0: Z.ty };
      } else {
        // swipe to switch
        gesture = { kind: 'swipe', x0: t.clientX, y0: t.clientY, t0: Date.now(), moved: false };
      }
    }
  }, { passive: false });

  lbStage.addEventListener('touchmove', (e) => {
    if (!gesture) return;
    if (gesture.kind === 'pinch' && e.touches.length === 2) {
      e.preventDefault();
      const d = distance(e.touches[0], e.touches[1]);
      let s = Math.max(1, Math.min(10, gesture.s0 * (d / gesture.d0)));
      Z.scale = s;
      // keep the pinch midpoint stable on the image
      if (s === 1) { Z.tx = 0; Z.ty = 0; }
      applyImgTransform();
    } else if (gesture.kind === 'pan' && e.touches.length === 1) {
      e.preventDefault();
      const t = e.touches[0];
      Z.tx = gesture.tx0 + (t.clientX - gesture.x0);
      Z.ty = gesture.ty0 + (t.clientY - gesture.y0);
      applyImgTransform();
    } else if (gesture.kind === 'swipe' && e.touches.length === 1) {
      const t = e.touches[0];
      const dx = Math.abs(t.clientX - gesture.x0);
      const dy = Math.abs(t.clientY - gesture.y0);
      if (dx > 8 && dx > dy) gesture.moved = true;
    }
  }, { passive: false });

  lbStage.addEventListener('touchend', (e) => {
    if (!gesture) return;
    if (gesture.kind === 'swipe') {
      const t = e.changedTouches[0];
      const dx = t.clientX - gesture.x0;
      const dy = t.clientY - gesture.y0;
      const dt = Date.now() - gesture.t0;
      // require a fast-ish, mostly-horizontal swipe
      if (gesture.moved && Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy) && dt < 700) {
        stepLB(dx < 0 ? 1 : -1);
      }
    }
    gesture = null;
  });

  // double-tap toggles 2.5x zoom on image lightbox (parallels dblclick)
  let lastTap = 0;
  lbImg.addEventListener('touchend', (e) => {
    if (lbRoot.classList.contains('hidden')) return;
    const now = Date.now();
    if (now - lastTap < 320 && e.changedTouches.length === 1) {
      e.preventDefault();
      if (Z.scale > 1) { resetTouchZoom(); }
      else {
        const t = e.changedTouches[0];
        const r = lbImg.getBoundingClientRect();
        Z.scale = 2.5;
        Z.tx = (r.left + r.width / 2 - t.clientX) * (Z.scale - 1) / Z.scale;
        Z.ty = (r.top + r.height / 2 - t.clientY) * (Z.scale - 1) / Z.scale;
        applyImgTransform();
      }
    }
    lastTap = now;
  });

  // ---------------- trash-view touch nav ----------------
  const trashView = $('trash-view');
  if (trashView) {
    const tvStage = trashView.querySelector('.tv-stage');
    let tvG = null;
    tvStage.addEventListener('touchstart', (e) => {
      if (trashView.classList.contains('hidden')) return;
      if (e.touches.length !== 1) return;
      const t = e.touches[0];
      tvG = { x0: t.clientX, y0: t.clientY, moved: false, t0: Date.now() };
    });
    tvStage.addEventListener('touchmove', (e) => {
      if (!tvG) return;
      const t = e.touches[0];
      if (Math.abs(t.clientX - tvG.x0) > 8) tvG.moved = true;
    });
    tvStage.addEventListener('touchend', (e) => {
      if (!tvG) return;
      const t = e.changedTouches[0];
      const dx = t.clientX - tvG.x0;
      const dy = t.clientY - tvG.y0;
      const dt = Date.now() - tvG.t0;
      if (tvG.moved && Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy) && dt < 700) {
        const dir = dx < 0 ? 'next' : 'prev';
        const btn = trashView.querySelector('[data-tv="' + dir + '"]');
        if (btn) btn.click();
      }
      tvG = null;
    });
  }

  // ---------------- suppress synthetic mouse events during touch ----------------
  // The lasso in grid.js listens on mousedown. Mobile browsers can synthesize
  // mousedown after a touch, which would fight long-press and swipe. We let
  // click still fire (tap-to-open) but swallow mousedown while a touch is live.
  (function suppressSyntheticMouse() {
    let touchFlag = false;
    const scope = $('grid-scroll');
    if (!scope) return;
    scope.addEventListener('touchstart', () => { touchFlag = true; }, { passive: true, capture: true });
    scope.addEventListener('touchend', () => {
      setTimeout(() => { touchFlag = false; }, 500);
    }, { passive: true, capture: true });
    scope.addEventListener('touchcancel', () => {
      setTimeout(() => { touchFlag = false; }, 500);
    }, { passive: true, capture: true });
    scope.addEventListener('mousedown', (e) => {
      if (touchFlag) e.stopImmediatePropagation();
    }, true);
  })();

  // ---------------- long-press-to-select on grid tiles ----------------
  const gridScroll = $('grid-scroll');
  if (gridScroll && window.Grid) {
    let lp = null;             // long-press state
    const LONG_MS = 500;
    const MOVE_SLOP = 10;

    function cancelPress(clearClass) {
      if (!lp) return;
      clearTimeout(lp.timer);
      if (clearClass && lp.tile) lp.tile.classList.remove('long-pressing');
      lp = null;
    }

    gridScroll.addEventListener('touchstart', (e) => {
      if (e.touches.length !== 1) { cancelPress(true); return; }
      const t = e.touches[0];
      const tile = t.target.closest ? t.target.closest('.tile') : null;
      if (!tile || tile.classList.contains('ghost')) return;
      if (t.target.closest && t.target.closest('.tile-acts')) return;
      lp = {
        name: tile.dataset.name,
        x0: t.clientX, y0: t.clientY,
        tile: tile,
        fired: false,
        timer: setTimeout(() => {
          if (!lp) return;
          lp.fired = true;
          lp.tile.classList.add('long-pressing');
          // toggle selection + mark anchor so later shift-tap could extend
          const sel = Grid.selection;
          if (sel.has(lp.name)) sel.delete(lp.name); else sel.add(lp.name);
          Grid.anchor = Grid.indexOf(lp.name);
          Grid.render && Grid.render();
          Grid.onSelectionChange && Grid.onSelectionChange();
          if (navigator.vibrate) { try { navigator.vibrate(14); } catch (err) { /* ignore */ } }
        }, LONG_MS),
      };
    }, { passive: true });

    gridScroll.addEventListener('touchmove', (e) => {
      if (!lp) return;
      const t = e.touches[0];
      if (!t) return;
      if (Math.abs(t.clientX - lp.x0) + Math.abs(t.clientY - lp.y0) > MOVE_SLOP) {
        cancelPress(true);
      }
    }, { passive: true });

    gridScroll.addEventListener('touchend', (e) => {
      if (!lp) return;
      // if long-press fired, eat the tap so the lightbox doesn't open
      if (lp.fired) {
        e.preventDefault();
        e.stopPropagation();
        const tile = lp.tile;
        setTimeout(() => tile && tile.classList.remove('long-pressing'), 120);
      }
      cancelPress(false);
    }, { passive: false });

    gridScroll.addEventListener('touchcancel', () => cancelPress(true), { passive: true });

    // when a tap arrives on an already-selected tile with any selection active,
    // treat it as toggling selection (so you can un-pick on mobile too)
    const clickHandler = (e) => {
      if (!isMobile()) return;
      const tile = e.target.closest ? e.target.closest('.tile') : null;
      if (!tile || tile.classList.contains('ghost')) return;
      if (e.target.closest && e.target.closest('.tile-acts')) return;
      const name = tile.dataset.name;
      if (Grid.selection.size > 0) {
        // in multi-select mode, tap toggles instead of opening the lightbox
        e.stopPropagation();
        if (Grid.selection.has(name)) Grid.selection.delete(name);
        else Grid.selection.add(name);
        Grid.anchor = Grid.indexOf(name);
        Grid.render && Grid.render();
        Grid.onSelectionChange && Grid.onSelectionChange();
      }
    };
    gridScroll.addEventListener('click', clickHandler, true);
  }

  // ---------------- auto-close drawer when a prompt opens a modal ----------------
  // dialogs sit above the drawer anyway, but closing it keeps the picture clean
  const observerDrawer = new MutationObserver(() => {
    const anyOpen = !!document.querySelector('.overlay:not(.hidden)');
    if (anyOpen && panel.classList.contains('drawer-open')) closeDrawer();
  });
  document.querySelectorAll('.overlay').forEach((ov) => {
    observerDrawer.observe(ov, { attributes: true, attributeFilter: ['class'] });
  });

  // ---------------- clamp grid-cols on narrow screens so tiles stay tappable ----
  function clampCols() {
    const w = window.innerWidth;
    if (!window.MG || !window.MG.cfg) return;
    const slider = $('grid-cols');
    let maxCols = 10;
    if (w < 420) maxCols = 3;
    else if (w < 560) maxCols = 4;
    else if (w < 760) maxCols = 6;
    if (slider) slider.max = String(maxCols);
    if (MG.cfg.grid_cols > maxCols && window.Grid) {
      Grid.setCols(maxCols);
      if (slider) { slider.value = String(maxCols); }
      const v = $('grid-cols-val'); if (v) v.textContent = String(maxCols);
    }
  }
  window.addEventListener('resize', clampCols);
  // run once the UI is wired
  const bootCheck = setInterval(() => {
    if (window.MG && window.MG.cfg) { clampCols(); clearInterval(bootCheck); }
  }, 80);
})();
