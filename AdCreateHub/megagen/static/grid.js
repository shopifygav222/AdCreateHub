/* Virtualized media grid: windowed rendering, selection, lasso, hover previews.
   Only visible rows (±2 buffer) exist in the DOM, so 1,000+ tiles stay at 60fps. */
(function () {
  'use strict';

  const GAP = 10;
  const BUFFER_ROWS = 2;

  const Grid = {
    items: [],
    selection: new Set(),
    busy: new Set(),          // names with a Regenerate in flight
    cols: 6,
    tile: 160,
    rowH: 170,
    anchor: null,
    project: '',
    tab: 'images',
    onOpen: function () {},
    onAction: function () {},
    onSelectionChange: function () {},
  };
  window.Grid = Grid;

  let scrollEl, gridEl, lassoEl;
  const pool = new Map();     // name -> tile element
  let preview = null;         // the one live hover preview {name, el}
  let raf = 0;

  function byName(name) { return Grid.items.find((i) => i.name === name); }
  function indexOf(name) { return Grid.items.findIndex((i) => i.name === name); }
  Grid.byName = byName;
  Grid.indexOf = indexOf;

  // ---------- layout ----------
  function measure() {
    const w = gridEl.clientWidth;
    Grid.tile = Math.max(60, Math.floor((w - GAP * (Grid.cols - 1)) / Grid.cols));
    Grid.rowH = Grid.tile + GAP;
    const rows = Math.ceil(Grid.items.length / Grid.cols);
    gridEl.style.height = Math.max(rows * Grid.rowH - GAP, 0) + 'px';
  }

  function posOf(idx) {
    const row = Math.floor(idx / Grid.cols);
    const col = idx % Grid.cols;
    return { x: col * (Grid.tile + GAP), y: row * Grid.rowH };
  }

  // ---------- tiles ----------
  function mediaUrl(kind, item) {
    const base = kind === 'preview' ? '/preview/' : kind === 'full' ? '/media/' : '/thumb/';
    return base + encodeURIComponent(Grid.project) + '/' + encodeURIComponent(item.name)
      + '?v=' + (item.mtime || 0);
  }
  Grid.mediaUrl = mediaUrl;

  function actBtn(act, icon, title) {
    return '<button data-act="' + act + '" title="' + title + '">' + icon + '</button>';
  }

  function buildTile(item) {
    const el = document.createElement('div');
    el.className = 'tile';
    el.dataset.name = item.name;
    // stamped at build time: Regenerate reuses the filename, so a changed mtime
    // is the only signal that this tile's thumbnail is stale
    el.dataset.mtime = item.mtime;
    if (item.ghost) {
      el.classList.add('ghost');
      el.innerHTML = '<div class="tile-busy"><div class="spin"></div><span>generating…</span></div>'
        + '<span class="tile-badge">#' + item.n + '</span>';
      return el;
    }
    const isVid = item.kind === 'video';
    const copyTitle = isVid ? 'Copy prompt text' : 'Copy image';
    let html = '<img class="tm" loading="lazy" alt="" src="' + mediaUrl('thumb', item) + '">';
    if (isVid) {
      html += '<div class="tile-play">▶</div>';
      if (item.dur) html += '<span class="tile-dur">' + item.dur + 's</span>';
    }
    html += '<span class="tile-badge">#' + item.n + '</span>';
    html += '<div class="tile-acts">'
      + actBtn('regen', '🔄', 'Regenerate — replace this one')
      + actBtn('new', '✨', 'New — same generation, added next to it')
      + actBtn('edit', '✏️', 'Edit prompt — generate a new one next to it')
      + actBtn('copy', '📋', copyTitle)
      + actBtn('delete', '🗑', 'Delete')
      + '</div>';
    el.innerHTML = html;
    return el;
  }

  function paintState(el, item) {
    el.classList.toggle('sel', Grid.selection.has(item.name));
    const busy = Grid.busy.has(item.name);
    let overlay = el.querySelector('.tile-busy');
    if (busy && !overlay && !item.ghost) {
      overlay = document.createElement('div');
      overlay.className = 'tile-busy';
      overlay.innerHTML = '<div class="spin"></div><span>regenerating…</span>';
      el.appendChild(overlay);
    } else if (!busy && overlay && !item.ghost) {
      overlay.remove();
    }
  }

  function render() {
    raf = 0;
    if (!Grid.items.length) { clearPool(); return; }
    const top = scrollEl.scrollTop;
    const viewH = scrollEl.clientHeight;
    const firstRow = Math.max(0, Math.floor(top / Grid.rowH) - BUFFER_ROWS);
    const lastRow = Math.ceil((top + viewH) / Grid.rowH) + BUFFER_ROWS;
    const start = firstRow * Grid.cols;
    const end = Math.min(Grid.items.length, lastRow * Grid.cols);

    const needed = new Set();
    for (let i = start; i < end; i++) {
      const item = Grid.items[i];
      if (!item) continue;
      needed.add(item.name);
      let el = pool.get(item.name);
      if (!el) {
        el = buildTile(item);
        pool.set(item.name, el);
        gridEl.appendChild(el);
      }
      const p = posOf(i);
      el.style.width = Grid.tile + 'px';
      el.style.height = Grid.tile + 'px';
      el.style.transform = 'translate(' + p.x + 'px,' + p.y + 'px)';
      paintState(el, item);
    }
    [...pool.keys()].forEach((name) => { if (!needed.has(name)) dropTile(name); });
  }

  function clearPool() {
    stopPreview();
    pool.forEach((el) => el.remove());
    pool.clear();
  }

  function schedule() {
    if (!raf) raf = requestAnimationFrame(render);
  }
  Grid.render = schedule;

  function dropTile(name) {
    const el = pool.get(name);
    if (!el) return;
    if (preview && preview.name === name) stopPreview();
    el.remove();
    pool.delete(name);
  }

  Grid.setItems = function (items, opts) {
    const o = opts || {};
    Grid.project = o.project || Grid.project;
    Grid.tab = o.tab || Grid.tab;
    const live = new Set(items.map((i) => i.name));
    Grid.selection.forEach((n) => { if (!live.has(n)) Grid.selection.delete(n); });
    // drop pooled tiles whose file changed (Regenerate reuses the filename)
    items.forEach((i) => {
      const el = pool.get(i.name);
      if (el && el.dataset.mtime !== String(i.mtime)) dropTile(i.name);
    });
    [...pool.keys()].forEach((name) => { if (!live.has(name)) dropTile(name); });
    Grid.items = items;
    measure();
    render();
    Grid.onSelectionChange();
  };

  Grid.setCols = function (cols) {
    Grid.cols = Math.max(3, Math.min(10, parseInt(cols, 10) || 6));
    measure();
    schedule();
  };

  Grid.setBusy = function (names) {
    Grid.busy = new Set(names);
    schedule();
  };

  Grid.scrollToPrompt = function (n) {
    const idx = Grid.items.findIndex((i) => i.n === n);
    if (idx < 0) return;
    scrollEl.scrollTo({ top: Math.max(0, posOf(idx).y - 20), behavior: 'smooth' });
  };

  Grid.clearSelection = function () {
    if (!Grid.selection.size) return;
    Grid.selection.clear();
    schedule();
    Grid.onSelectionChange();
  };

  Grid.selectAll = function () {
    Grid.items.forEach((i) => { if (!i.ghost) Grid.selection.add(i.name); });
    schedule();
    Grid.onSelectionChange();
  };

  // ---------- hover previews ----------
  function stopPreview() {
    if (!preview) return;
    try { preview.el.pause(); } catch (e) { /* ignore */ }
    preview.el.removeAttribute('src');
    preview.el.remove();
    preview = null;
  }

  function startPreview(tileEl, item) {
    stopPreview();                                  // never two at once
    const v = document.createElement('video');
    v.className = 'tp';
    v.muted = true;
    v.loop = true;
    v.autoplay = true;
    v.preload = 'none';
    v.src = mediaUrl('preview', item);
    v.addEventListener('error', () => { if (preview && preview.el === v) stopPreview(); });
    tileEl.appendChild(v);
    preview = { name: item.name, el: v };
    const p = v.play();
    if (p && p.catch) p.catch(() => {});
  }

  // ---------- events ----------
  function tileFrom(target) {
    return target.closest ? target.closest('.tile') : null;
  }

  function onClick(e) {
    if (suppressClick) { suppressClick = false; return; }   // §C9: this click just ended a lasso
    const tile = tileFrom(e.target);
    if (!tile) return;
    const name = tile.dataset.name;
    const item = byName(name);
    if (!item || item.ghost) return;
    const actBtnEl = e.target.closest('.tile-acts button');
    if (actBtnEl) {
      e.stopPropagation();
      Grid.onAction(actBtnEl.dataset.act, name);
      return;
    }
    const idx = indexOf(name);
    if (e.ctrlKey || e.metaKey) {
      if (Grid.selection.has(name)) Grid.selection.delete(name); else Grid.selection.add(name);
      Grid.anchor = idx;
      schedule();
      Grid.onSelectionChange();
      return;
    }
    if (e.shiftKey) {
      const from = Grid.anchor == null ? idx : Grid.anchor;
      const [a, b] = from <= idx ? [from, idx] : [idx, from];
      for (let i = a; i <= b; i++) {
        if (Grid.items[i] && !Grid.items[i].ghost) Grid.selection.add(Grid.items[i].name);
      }
      schedule();
      Grid.onSelectionChange();
      return;
    }
    Grid.anchor = idx;
    Grid.onOpen(name);
  }

  function onOver(e) {
    const tile = tileFrom(e.target);
    if (!tile) { if (preview) stopPreview(); return; }
    const item = byName(tile.dataset.name);
    if (!item || item.kind !== 'video' || item.ghost) { if (preview) stopPreview(); return; }
    if (preview && preview.name === item.name) return;
    if (!window.MG || !MG.cfg.hover_previews || !item.has_preview) { stopPreview(); return; }
    startPreview(tile, item);
  }

  function onOut(e) {
    if (!preview) return;
    const to = e.relatedTarget;
    if (to && to.closest && to.closest('.tile[data-name="' + CSS.escape(preview.name) + '"]')) return;
    stopPreview();
  }

  // lasso — §C9: starts anywhere in the grid, tiles included (Bananza parity).
  // A press that never moves past the 5px threshold is still a plain click.
  let lasso = null;
  let suppressClick = false;   // §C9: eats the click that ends a drag-lasso

  function onDown(e) {
    if (e.button !== 0) return;
    if (e.target.closest && e.target.closest('.tile-acts')) return;   // buttons need clean clicks
    const rect = scrollEl.getBoundingClientRect();
    lasso = {
      x0: e.clientX - rect.left + scrollEl.scrollLeft,
      y0: e.clientY - rect.top + scrollEl.scrollTop,
      base: (e.ctrlKey || e.metaKey || e.shiftKey) ? new Set(Grid.selection) : new Set(),
      moved: false,
      fromTile: !!tileFrom(e.target),   // a stationary press on a tile is a click, not a lasso
    };
    e.preventDefault();                 // also stops the native image ghost-drag
  }

  function gridOrigin() {
    // #grid sits inside the padded scroll box; tile coords are relative to it
    const gs = gridEl.getBoundingClientRect();
    const ss = scrollEl.getBoundingClientRect();
    return { x: gs.left - ss.left + scrollEl.scrollLeft, y: gs.top - ss.top + scrollEl.scrollTop };
  }

  function onMove(e) {
    if (!lasso) return;
    const rect = scrollEl.getBoundingClientRect();
    if (lasso.moved) {
      // §C9: auto-scroll when lassoing near the top/bottom edge — same 34px
      // band and 16px step Bananza uses. Only once the drag is real, so a
      // stationary press near an edge never scrolls. Mutate scrollTop BEFORE
      // computing x1/y1 so the rectangle uses the post-scroll coordinates.
      if (e.clientY > rect.bottom - 34) scrollEl.scrollTop += 16;
      else if (e.clientY < rect.top + 34 && scrollEl.scrollTop > 0) scrollEl.scrollTop -= 16;
    }
    const x1 = e.clientX - rect.left + scrollEl.scrollLeft;
    const y1 = e.clientY - rect.top + scrollEl.scrollTop;
    if (!lasso.moved && Math.abs(x1 - lasso.x0) + Math.abs(y1 - lasso.y0) < 5) return;
    lasso.moved = true;
    const L = Math.min(lasso.x0, x1), T = Math.min(lasso.y0, y1);
    const R = Math.max(lasso.x0, x1), B = Math.max(lasso.y0, y1);
    lassoEl.classList.remove('hidden');
    lassoEl.style.left = L + 'px';
    lassoEl.style.top = T + 'px';
    lassoEl.style.width = (R - L) + 'px';
    lassoEl.style.height = (B - T) + 'px';

    const o = gridOrigin();
    const sel = new Set(lasso.base);
    for (let i = 0; i < Grid.items.length; i++) {
      const it = Grid.items[i];
      if (it.ghost) continue;
      const p = posOf(i);
      const tl = o.x + p.x, tt = o.y + p.y;
      if (tl < R && tl + Grid.tile > L && tt < B && tt + Grid.tile > T) sel.add(it.name);
    }
    Grid.selection = sel;
    schedule();
    Grid.onSelectionChange();
  }

  function onUp() {
    if (!lasso) return;
    const moved = lasso.moved, fromTile = lasso.fromTile;
    lasso = null;
    lassoEl.classList.add('hidden');
    if (moved) {
      // §C9: the mouseup over a tile fires a click next — eat it so ending a
      // lasso can never open the fullscreen viewer (Bananza's 150ms trick).
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 150);
    } else if (!fromTile) {
      Grid.clearSelection();     // plain click on empty space still clears
    }
  }

  Grid.init = function () {
    scrollEl = document.getElementById('grid-scroll');
    gridEl = document.getElementById('grid');
    lassoEl = document.getElementById('lasso');
    scrollEl.addEventListener('scroll', schedule, { passive: true });
    // ResizeObserver, not window.resize: it also catches the panel collapsing
    const ro = new ResizeObserver(() => { measure(); schedule(); });
    ro.observe(scrollEl);
    gridEl.addEventListener('click', onClick);
    gridEl.addEventListener('dragstart', (e) => e.preventDefault());   // §C9: no image ghost-drag
    gridEl.addEventListener('mouseover', onOver);
    gridEl.addEventListener('mouseout', onOut);
    scrollEl.addEventListener('mousedown', onDown);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  };
})();
