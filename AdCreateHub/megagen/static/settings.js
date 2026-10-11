/* Settings modal: Google Cloud connection, performance, trash, spend tracker.
   app.js hands over a small bridge of shared helpers when it wires the page. */
(function () {
  'use strict';

  const Settings = {};
  window.Settings = Settings;

  let U = null;                 // {$, esc, jpost, api, toast, oops, confirmBox, refresh}
  let trashSel = null;
  let trashItems = [];          // §C4: the list the trash gallery shows, in order
  const TV = { idx: -1 };       // §C4: fullscreen trash viewer state

  Settings.sync = function () {
    const $ = U.$, cfg = window.MG.cfg;
    $('set-workers').value = cfg.workers;
    $('set-workers-val').textContent = cfg.workers;
    $('set-hover-previews').checked = !!cfg.hover_previews;
    $('hover-previews-view').checked = !!cfg.hover_previews;
    $('grid-cols').value = cfg.grid_cols;
    $('grid-cols-val').textContent = cfg.grid_cols;
    Settings.renderCapacity();
  };

  Settings.renderCapacity = function () {
    const cfg = window.MG.cfg;
    const n = (accts || []).filter((a) => a.enabled && a.project).length || 1;
    const per = Number(cfg.workers) || 3;
    U.$('capacity-line').textContent = n > 1
      ? per + ' at a time per account · ' + n + ' accounts → up to ' + (per * n) + ' in parallel'
      : per + ' at a time';
  };

  Settings.renderStats = function (u, trashCount) {
    const card = (v, l) => '<div class="stat-card"><div class="v">' + v + '</div><div class="l">' + l + '</div></div>';
    U.$('stat-cards').innerHTML =
      card(u.session_images, 'Items this session')
      + card('$' + u.session_cost.toFixed(2), 'Est. cost this session')
      + card(u.images, 'Items all time')
      + card('$' + u.est_cost.toFixed(2), 'Est. cost all time');
    U.$('trash-count-badge').textContent = trashCount;
  };

  Settings.loadTrash = async function () {
    const $ = U.$, esc = U.esc;
    try {
      const items = await U.api('/api/trash');
      trashItems = items;
      trashSel = null;
      $('trash-gallery').innerHTML = items.length ? items.map((t) =>
        '<div class="ti" data-name="' + esc(t.name) + '" title="' + esc(t.orig) + '">'
        + '<img src="/trash-thumb/' + encodeURIComponent(t.name) + '" alt="" loading="lazy">'
        + (t.kind === 'video' ? '<span class="tk">▶</span>' : '') + '</div>').join('')
        : '<div class="trash-empty">Trash is empty.</div>';
    } catch (e) { U.oops(e); }
  };

  // ---------- fullscreen trash viewer (v1.1 §C4) ----------
  function tvEls() {
    const root = document.getElementById('trash-view');
    return { root: root,
             img: root.querySelector('.tv-img'), vid: root.querySelector('.tv-vid'),
             name: root.querySelector('.tv-name'), proj: root.querySelector('.tv-proj') };
  }

  function tvPaint() {
    const t = trashItems[TV.idx];
    if (!t) { tvClose(); return; }
    const e = tvEls();
    const src = '/trash-media/' + encodeURIComponent(t.name);
    if (t.kind === 'video') {
      e.img.classList.add('hidden');
      e.img.removeAttribute('src');
      e.vid.classList.remove('hidden');
      if (e.vid.getAttribute('src') !== src) {
        e.vid.src = src;
        const p = e.vid.play();
        if (p && p.catch) p.catch(function () {});
      }
    } else {
      e.vid.classList.add('hidden');
      e.vid.pause();
      e.vid.removeAttribute('src');
      e.vid.load();
      e.img.classList.remove('hidden');
      if (e.img.getAttribute('src') !== src) e.img.src = src;
    }
    e.name.textContent = t.orig;
    e.proj.textContent = t.project ? 'from “' + t.project + '”' : '';
  }

  function tvOpen(name) {
    const i = trashItems.findIndex(function (t) { return t.name === name; });
    if (i < 0) return;
    TV.idx = i;
    tvEls().root.classList.remove('hidden');
    tvPaint();
  }

  function tvClose() {
    const e = tvEls();
    TV.idx = -1;
    e.root.classList.add('hidden');
    e.vid.pause();
    e.vid.removeAttribute('src');
    e.vid.load();                 // §C3: actually release the file handle
    e.img.removeAttribute('src');
  }

  function tvStep(d) {
    if (TV.idx < 0) return;
    const j = TV.idx + d;
    if (j < 0 || j >= trashItems.length) return;
    TV.idx = j;
    tvPaint();
  }

  let accts = [];
  let healthTimer = null;

  Settings.renderAccounts = function (rows, usage) {
    accts = rows || [];
    const $ = U.$, esc = U.esc;
    const seen = {};
    $('accounts-list').innerHTML = accts.map((a) => {
      const dupe = seen[a.project] && a.project;
      seen[a.project] = true;
      const spend = (usage && usage.per_account && usage.per_account[a.id]) || null;
      return '<div class="acct-card" data-id="' + esc(a.id) + '">'
        + '<div class="acct-top">'
        +   '<span class="acct-health ' + esc(a.status) + '"><i></i>' + esc(a.detail) + '</span>'
        +   '<input class="acct-label" data-f="label" value="' + esc(a.label) + '" maxlength="40">'
        +   '<label class="check-row" style="margin:0"><input type="checkbox" data-f="enabled"'
        +     (a.enabled ? ' checked' : '') + '> on</label>'
        + '</div>'
        + '<div class="acct-grid">'
        +   '<label class="wide">Project ID<input data-f="project" value="' + esc(a.project) + '" placeholder="my-project-448212" spellcheck="false"></label>'
        +   '<label class="narrow">Location<input data-f="location" value="' + esc(a.location) + '" spellcheck="false"></label>'
        +   '<label class="narrow">Credit $<input data-f="credit" type="number" min="0" step="1" value="' + a.credit + '"></label>'
        + '</div>'
        + (dupe ? '<div class="acct-dupe">Same project as an account above — no extra quota.</div>' : '')
        + '<div class="acct-foot">'
        +   '<button class="btn sm" data-act="test">Test</button>'
        +   '<button class="btn sm ghost" data-act="remove"' + (accts.length <= 1 ? ' disabled' : '') + '>Remove</button>'
        +   '<span class="acct-status">' + (spend ? '$' + spend.est_cost.toFixed(2) + ' spent · ' + spend.images + ' items' : '') + '</span>'
        + '</div>'
        + '</div>';
    }).join('');
    Settings.renderCapacity();
  };

  Settings.loadAccounts = async function () {
    try {
      const r = await U.api('/api/accounts');
      Settings.renderAccounts(r.accounts, r.usage);
    } catch (e) { U.oops(e); }
  };

  // in-flight counts change constantly; they must never churn DATA_VERSION,
  // so the panel polls for its own health only while it is open
  Settings.startHealthPoll = function () {
    Settings.stopHealthPoll();
    healthTimer = setInterval(() => {
      if (document.getElementById('settings-modal').classList.contains('hidden')) {
        Settings.stopHealthPoll();
        return;
      }
      if (!document.activeElement || !document.activeElement.closest('#accounts-list')) {
        Settings.loadAccounts();          // never repaint under the user's cursor
      }
    }, 2000);
  };
  Settings.stopHealthPoll = function () {
    if (healthTimer) { clearInterval(healthTimer); healthTimer = null; }
  };

  Settings.init = function (bridge) {
    U = bridge;
    const $ = U.$, jpost = U.jpost, toast = U.toast, oops = U.oops, MG = window.MG;

    $('btn-settings').onclick = () => {
      Settings.sync();
      Settings.loadTrash();
      Settings.loadAccounts();
      Settings.startHealthPoll();
      $('settings-modal').classList.remove('hidden');
    };
    $('btn-guide').onclick = () => $('guide-modal').classList.remove('hidden');

    $('btn-add-account').onclick = async () => {
      try {
        await jpost('/api/accounts', { label: 'Account ' + (accts.length + 1) });
        await Settings.loadAccounts();
        toast('Account added — paste its Project ID, then press Test.');
      } catch (e) { oops(e); }
    };

    $('accounts-list').addEventListener('change', async (e) => {
      const card = e.target.closest('.acct-card');
      const f = e.target.dataset.f;
      if (!card || !f) return;
      const value = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
      try {
        await U.api('/api/accounts/' + encodeURIComponent(card.dataset.id),
          { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ [f]: value }) });
        await Settings.loadAccounts();
        await U.refresh();
      } catch (err) { oops(err); }
    });

    $('accounts-list').addEventListener('click', async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const card = e.target.closest('.acct-card');
      const id = card.dataset.id;
      if (btn.dataset.act === 'test') {
        card.querySelector('.acct-status').textContent = 'Testing…';
        // a real Google round-trip is almost always slower than the 2s health
        // poll, which replaces #accounts-list's innerHTML on every tick — `card`
        // would go stale mid-await, so look the row up fresh before writing to it
        const statusEl = () => {
          const c = document.querySelector('.acct-card[data-id="' + CSS.escape(id) + '"]');
          return c ? c.querySelector('.acct-status') : null;
        };
        try {
          const r = await jpost('/api/accounts/' + encodeURIComponent(id) + '/test', {});
          const el = statusEl();
          if (el) el.textContent = r.message;
        } catch (err) {
          const el = statusEl();
          if (el) el.textContent = '❌ ' + err.message;
        }
        return;
      }
      U.confirmBox('Remove this account? Media it already made is untouched.', async () => {
        try {
          await U.api('/api/accounts/' + encodeURIComponent(id), { method: 'DELETE' });
          await Settings.loadAccounts();
          await U.refresh();
        } catch (err) { oops(err); }
      }, 'Remove');
    });

    $('set-workers').oninput = (e) => { $('set-workers-val').textContent = e.target.value; };
    $('set-workers').onchange = async (e) => {
      MG.cfg.workers = Number(e.target.value);
      Settings.renderCapacity();
      try { await jpost('/api/config', { workers: MG.cfg.workers }); } catch (err) { oops(err); }
    };

    const setHover = async (on) => {
      MG.cfg.hover_previews = on;
      $('set-hover-previews').checked = on;
      $('hover-previews-view').checked = on;
      try { await jpost('/api/config', { hover_previews: on }); } catch (e) { oops(e); }
    };
    $('set-hover-previews').onchange = (e) => setHover(e.target.checked);
    $('hover-previews-view').onchange = (e) => setHover(e.target.checked);

    $('btn-reset-usage').onclick = () => U.confirmBox('Reset the spend tracker to zero?', async () => {
      try { await jpost('/api/usage/reset', {}); await U.refresh(); } catch (e) { oops(e); }
    }, 'Reset');

    $('trash-gallery').addEventListener('click', (e) => {
      const ti = e.target.closest('.ti');
      if (!ti) return;
      trashSel = ti.dataset.name;
      $('trash-gallery').querySelectorAll('.ti').forEach((x) => x.classList.toggle('on', x === ti));
    });

    $('btn-trash-restore').onclick = async () => {
      tvClose();
      if (!trashSel) { toast('Tap an item in the trash first.', 'err'); return; }
      try {
        const r = await jpost('/api/trash/restore', { name: trashSel, project: MG.project });
        toast('Restored ' + r.name + ' to “' + MG.project + '”.');
        await Settings.loadTrash();
        await U.refresh();
      } catch (e) { oops(e); }
    };

    $('btn-trash-purge').onclick = async () => {
      tvClose();
      if (!trashSel) { toast('Tap an item in the trash first.', 'err'); return; }
      try {
        await jpost('/api/trash/purge', { name: trashSel });
        toast('Deleted forever.');
        await Settings.loadTrash();
        await U.refresh();
      } catch (e) { oops(e); }
    };

    $('btn-trash-empty').onclick = () => $('trash-confirm').classList.remove('hidden');
    $('btn-trash-empty-no').onclick = () => $('trash-confirm').classList.add('hidden');
    $('btn-trash-empty-yes').onclick = async () => {
      tvClose();
      $('trash-confirm').classList.add('hidden');
      try {
        const r = await jpost('/api/trash/empty', {});
        let msg = r.purged ? 'Trash emptied — ' + r.purged + ' item(s) gone for good.'
          : 'Trash was already empty.';
        if (r.locked && r.locked.length) {
          msg = 'Emptied ' + r.purged + ' item(s). ' + r.locked.length
            + " couldn't be deleted because Windows still has them open"
            + ' (a thumbnail may be building) — try again in a moment.';
        }
        toast(msg, r.locked && r.locked.length ? 'err' : null);
        await Settings.loadTrash();
        await U.refresh();
      } catch (e) { oops(e); }
    };

    // ---------- §C4: trash viewer wiring ----------
    $('trash-gallery').addEventListener('dblclick', (e) => {
      const ti = e.target.closest('.ti');
      if (!ti) return;
      trashSel = ti.dataset.name;
      $('trash-gallery').querySelectorAll('.ti').forEach((x) => x.classList.toggle('on', x === ti));
      tvOpen(ti.dataset.name);
    });

    const tvRoot = document.getElementById('trash-view');
    tvRoot.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-tv]');
      if (b) {
        const t = trashItems[TV.idx];
        if (b.dataset.tv === 'close') { tvClose(); return; }
        if (b.dataset.tv === 'prev') { tvStep(-1); return; }
        if (b.dataset.tv === 'next') { tvStep(1); return; }
        if (!t) return;
        tvClose();                              // §C3: release the handle FIRST
        try {
          if (b.dataset.tv === 'restore') {
            const r = await jpost('/api/trash/restore', { name: t.name, project: MG.project });
            toast('Restored ' + r.name + ' to “' + MG.project + '”.');
          } else if (b.dataset.tv === 'purge') {
            await jpost('/api/trash/purge', { name: t.name });
            toast('Deleted forever.');
          }
          await Settings.loadTrash();
          await U.refresh();
        } catch (err) { oops(err); }
        return;
      }
      if (e.target === tvRoot || e.target.classList.contains('tv-stage')) tvClose();
    });

    // capture phase so Esc closes the VIEWER, never the settings modal under it
    document.addEventListener('keydown', (e) => {
      if (TV.idx < 0) return;
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); tvClose(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); e.stopPropagation(); tvStep(1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); e.stopPropagation(); tvStep(-1); }
    }, true);
  };
})();
