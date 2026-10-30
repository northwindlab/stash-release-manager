// ==UserScript==
// @name         StashReleaseManager for stash-box sites
// @namespace    stash-release-manager
// @version      0.246.12
// @description  On scene pages and scene cards: Send to wishlist, Send and search, Open in Stash, Copy Stash-ID, plus a status colour. On performer and studio pages: Watch / Unwatch.
// @match        https://stashdb.org/*
// @grant        GM_xmlhttpRequest
// @grant        GM_setClipboard
// @connect      __SRM_HOST__
// @updateURL    __SRM_URL__/userscript/srm.user.js
// @downloadURL  __SRM_URL__/userscript/srm.user.js
// ==/UserScript==

(function () {
  'use strict';

  const SRM = '__SRM_URL__';
  const SCENE_PATH = /^\/scenes\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;
  // The colours of the Release Calendar pills: green imported / in the library, purple on the wishlist, blue found by a scan, red import failed;
  // grey = dismissed; no colour = the sidecar does not know the scene.
  const COLORS = { in_library: '#5cc98a', imported: '#5cc98a', sent_to_whisparr: '#aa87de', known: '#6fa8e0', import_failed: '#e05c5c', dismissed: '#8a8f98', none: 'rgba(255,255,255,.35)' };
  const LABELS = { in_library: 'In your Stash library', imported: 'Imported', sent_to_whisparr: 'On the wishlist', known: 'Found by a scan, not on the wishlist yet', import_failed: 'Import failed', dismissed: 'Dismissed', none: 'Not in Stash, not known to StashReleaseManager' };

  const ENTITY_PATH = /^\/(performers|studios)\/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/;
  const SHORT = { imported: 'Imported', sent_to_whisparr: 'Wishlist', known: 'Scan', import_failed: 'Failed', dismissed: 'Dismissed' };
  const GREY = 'rgba(255,255,255,.4)';
  const GREEN = '#5cc98a';
  const state = { scenes: {}, stashUrl: '', entities: {} };

  function call(method, path, body) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method, url: SRM + path,
        headers: body ? { 'Content-Type': 'application/json' } : {},
        data: body ? JSON.stringify(body) : undefined,
        timeout: 120000,
        onload: (r) => {
          let data = null;
          try { data = JSON.parse(r.responseText); } catch (e) { /* not JSON */ }
          if (r.status >= 200 && r.status < 300) resolve(data);
          else reject(new Error((data && data.detail) || ('HTTP ' + r.status)));
        },
        onerror: () => reject(new Error('StashReleaseManager is not reachable')),
        ontimeout: () => reject(new Error('StashReleaseManager did not answer in time')),
      });
    });
  }

  const sceneIdOf = (href) => {
    try {
      const m = SCENE_PATH.exec(new URL(href, location.origin).pathname);
      return m ? m[1] : null;
    } catch (e) { return null; }
  };

  function statusKey(id) {
    const s = state.scenes[id];
    return s && s.status ? s.status : 'none';
  }

  async function refresh(ids) {
    const wanted = [...new Set(ids)].filter(Boolean);
    for (let i = 0; i < wanted.length; i += 100) {
      const chunk = wanted.slice(i, i + 100);
      try {
        const r = await call('GET', '/userscript/state?origin=' + encodeURIComponent(location.origin) + '&ids=' + chunk.join(','));
        state.stashUrl = r.stash_url || state.stashUrl;
        Object.assign(state.scenes, r.scenes);
      } catch (e) {
        chunk.forEach((id) => { state.scenes[id] = { error: e.message }; });
      }
    }
    paint();
  }

  const ICONS = {
    send: 'M12 5v14M5 12h14',
    search: 'M10 4a6 6 0 1 0 0 12 6 6 0 0 0 0-12zM15 15l5 5M10 7v6M7 10h6',
    stash: 'M5 3h14v18H5zM9 8h6M9 12h6M9 16h4',
    copy: 'M8 8h11v13H8zM5 16V3h11',
    watch: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z',
    unwatch: 'M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6zM4 4l16 16',
  };

  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('stroke', 'currentColor');
    svg.setAttribute('stroke-width', '2');
    svg.setAttribute('stroke-linecap', 'round');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', ICONS[name]);
    svg.appendChild(path);
    return svg;
  }

  function button(kind, title, onClick, text) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'srm-btn srm-btn-' + kind;
    b.title = title;
    b.appendChild(icon(kind));
    if (text) b.appendChild(document.createTextNode(' ' + text));
    b.addEventListener('click', (ev) => { ev.preventDefault(); ev.stopPropagation(); onClick(b); });
    return b;
  }

  // A short pop-up in the corner (like Stasharr's): the hand-off worked or failed. Errors stay a little longer.
  function toast(text, ok) {
    let box = document.querySelector('.srm-toasts');
    if (!box) {
      box = document.createElement('div');
      box.className = 'srm-toasts';
      document.body.appendChild(box);
    }
    const t = document.createElement('div');
    t.className = 'srm-toast ' + (ok ? 'srm-toast-ok' : 'srm-toast-err');
    t.textContent = text;
    box.appendChild(t);
    setTimeout(() => t.remove(), ok ? 4000 : 8000);
  }

  async function send(id, search, btn) {
    btn.disabled = true;
    try {
      const r = await call('POST', '/userscript/wishlist?search=' + (search ? 'true' : 'false'), { origin: location.origin, id });
      btn.title = r && r.already_in_library ? 'Stash already has this scene' : 'Done';
      btn.classList.remove('srm-failed');
      if (r && r.already_in_library) toast('Stash already has this scene.', true);
      else if (r && r.already_sent) toast('Already on the wishlist.', true);
      else toast(search ? 'Sent to StashReleaseManager: added to the wishlist, searching now.' : 'Sent to StashReleaseManager: added to the wishlist.', true);
    } catch (e) {
      btn.title = 'Failed: ' + e.message;
      btn.classList.add('srm-failed');
      toast('Could not send the scene: ' + e.message, false);
    }
    btn.disabled = false;
    await refresh([id]);
  }

  async function copyId(id, btn) {
    GM_setClipboard(id, 'text');
    const old = btn.title;
    btn.title = 'Copied';
    btn.classList.add('srm-done');
    setTimeout(() => { btn.title = old; btn.classList.remove('srm-done'); }, 1500);
  }

  // A short label with a coloured dot; the long explanation is the tooltip.
  function chip(name) {
    const c = document.createElement('span');
    c.className = 'srm-chip';
    const dot = document.createElement('span');
    dot.className = 'srm-chip-dot';
    const text = document.createElement('span');
    text.className = 'srm-chip-text';
    text.textContent = name;
    c.append(dot, text);
    c.dataset.name = name;
    return c;
  }

  function setChip(c, color, title, word) {
    c.querySelector('.srm-chip-dot').style.background = color;
    c.querySelector('.srm-chip-text').textContent = word ? c.dataset.name + ' ' + word : c.dataset.name;
    c.title = title;
  }

  function bar(id) {
    const wrap = document.createElement('span');
    wrap.className = 'srm-bar';
    wrap.dataset.srmId = id;
    const dot = document.createElement('span');
    dot.className = 'srm-dot';
    wrap.append(dot, chip('Stash'), chip('SRM'),
      button('send', 'Send to wishlist (searched by the schedule)', (b) => send(id, false, b)),
      button('search', 'Send to wishlist and search now', (b) => send(id, true, b)),
      button('stash', 'Open in Stash', () => {
        const s = state.scenes[id];
        if (s && s.stash_scene_id) window.open(state.stashUrl + '/scenes/' + s.stash_scene_id, '_blank', 'noopener');
      }),
      button('copy', 'Copy Stash-ID', (b) => copyId(id, b)),
    );
    return wrap;
  }

  function paint() {
    document.querySelectorAll('.srm-bar[data-srm-id]').forEach((wrap) => {
      try { paintScene(wrap); } catch (e) { console.warn('StashReleaseManager userscript:', e); }
    });
    try { paintEntities(); } catch (e) { console.warn('StashReleaseManager userscript:', e); }
  }

  function paintScene(wrap) {
    const id = wrap.dataset.srmId;
    const key = statusKey(id);
    const s = state.scenes[id] || {};
    const loading = !state.scenes[id];
    wrap.classList.toggle('srm-loading', loading);
    wrap.style.setProperty('--srm-color', loading ? GREY : s.error ? '#777' : COLORS[key]);
    wrap.title = loading ? 'Loading the status ...' : s.error ? 'StashReleaseManager: ' + s.error : LABELS[key];
    const inLib = key === 'in_library';
    const wished = key === 'sent_to_whisparr' || key === 'imported';
    const chips = wrap.querySelectorAll('.srm-chip');
    if (loading) {
      chips.forEach((c) => setChip(c, GREY, 'Loading the status ...'));
    } else if (chips.length) {
      setChip(chips[0], s.stash_scene_id ? GREEN : GREY, s.stash_scene_id ? 'Stash has this scene with a file' : 'Not in your Stash library');
      setChip(chips[1], s.srm ? COLORS[s.srm] : GREY, s.srm ? LABELS[s.srm] : 'StashReleaseManager does not know this scene', SHORT[s.srm] || '');
    }
    wrap.querySelector('.srm-btn-send').disabled = loading || inLib || wished || !!s.error;
    wrap.querySelector('.srm-btn-search').disabled = loading || inLib || !!s.error;
    wrap.querySelector('.srm-btn-stash').style.display = s.stash_scene_id ? '' : 'none';
  }


  // Performer / studio pages: Watch / Unwatch and Open in Stash.
  async function refreshEntity(kind, id) {
    const key = kind + ':' + id;
    try {
      state.entities[key] = await call('GET', '/userscript/entity-state?origin=' + encodeURIComponent(location.origin) + '&kind=' + kind + '&id=' + id);
      state.stashUrl = state.entities[key].stash_url || state.stashUrl;
    } catch (e) {
      state.entities[key] = { error: e.message };
    }
    paint();
  }

  function entityName(h3) {
    const span = h3.querySelector('span');
    return ((span && span.textContent) || h3.textContent || '').trim();
  }

  function entityBar(kind, id, h3) {
    const key = kind + ':' + id;
    const wrap = document.createElement('span');
    wrap.className = 'srm-bar srm-page srm-entity';
    wrap.dataset.srmEntity = key;
    const toggle = button('watch', '', async (b) => {
      const e = state.entities[key] || {};
      b.disabled = true;
      try {
        await call('POST', '/userscript/watch', { origin: location.origin, kind, id, name: entityName(h3), watched: !e.watched });
        const name = entityName(h3) || kind;
        toast(e.watched ? 'Removed from the watchlist: ' + name : 'Added to the watchlist: ' + name, true);
      } catch (err) {
        state.entities[key] = Object.assign({}, e, { error: err.message });
        toast('Could not change the watchlist: ' + err.message, false);
      }
      b.disabled = false;
      await refreshEntity(kind, id);
    });
    toggle.classList.add('srm-btn-toggle');
    const open = button('stash', 'Open in Stash', () => {
      const e = state.entities[key];
      if (e && e.local_id) window.open(state.stashUrl + '/' + kind + 's/' + e.local_id, '_blank', 'noopener');
    });
    wrap.append(chip('Stash'), chip('SRM'), toggle, open);
    return wrap;
  }

  function paintEntities() {
    document.querySelectorAll('.srm-entity').forEach((wrap) => {
      const e = state.entities[wrap.dataset.srmEntity] || {};
      const loading = !state.entities[wrap.dataset.srmEntity];
      wrap.classList.toggle('srm-loading', loading);
      const chips = wrap.querySelectorAll('.srm-chip');
      const toggle = wrap.querySelector('.srm-btn-toggle');
      wrap.style.setProperty('--srm-color', e.error ? '#777' : GREY);
      toggle.disabled = !!e.error || e.watched === undefined;
      toggle.replaceChildren(icon(e.watched ? 'unwatch' : 'watch'));
      const how = e.mode === 'tag' ? 'the "' + (e.tag_name || 'watchlist') + '" tag' : 'the Favorite flag';
      toggle.title = e.watched ? 'Unwatch: take it off the watchlist (removes ' + how + ' in Stash, nothing is deleted)'
        : 'Watch: put it on the watchlist (sets ' + how + ' in Stash; created there when missing)';
      if (loading) {
        chips.forEach((c) => setChip(c, GREY, 'Loading the status ...'));
        toggle.title = 'Loading the status ...';
        wrap.querySelector('.srm-btn-stash').style.display = 'none';
        return;
      }
      setChip(chips[0], e.local_id ? GREEN : GREY, e.error ? 'StashReleaseManager: ' + e.error : e.local_id ? 'Stash has this entry' : 'Not in your Stash');
      setChip(chips[1], e.watched ? GREEN : GREY, e.watched ? 'On the watchlist' : 'Not on the watchlist');
      wrap.querySelector('.srm-btn-stash').style.display = e.local_id ? '' : 'none';
    });
  }

  let pending = null;
  function scan() {
    pending = null;
    const ids = [];
    // Scene page: next to the heading of the scene info card.
    const pageId = sceneIdOf(location.pathname);
    const head = document.querySelector('.scene-info .card-header');
    const old = head && head.querySelector('.srm-bar');
    if (old && old.dataset.srmId !== pageId) old.remove();
    if (pageId && head && !head.querySelector('.srm-bar')) {
      const b = bar(pageId);
      b.classList.add('srm-page');
      const h6 = head.querySelector('h6');
      (h6 || head).insertAdjacentElement(h6 ? 'afterend' : 'beforeend', b);
      ids.push(pageId);
    }
    const em = ENTITY_PATH.exec(location.pathname);
    if (em) {
      const kind = em[1] === 'studios' ? 'studio' : 'performer';
      const h3 = document.querySelector(kind === 'studio' ? '.studio-title h3' : '.PerformerInfo h3');
      const stale = document.querySelector('.srm-entity');
      if (stale && stale.dataset.srmEntity !== kind + ':' + em[2]) stale.remove();
      if (h3 && !document.querySelector('.srm-entity')) {
        h3.insertAdjacentElement('afterend', entityBar(kind, em[2], h3));
        refreshEntity(kind, em[2]);
      }
    }
    // Scene cards in lists.
    document.querySelectorAll('.SceneCard').forEach((card) => {
      if (card.querySelector('.srm-bar')) return;
      const link = card.querySelector('a.SceneCard-image, a[href^="/scenes/"]');
      const id = link && sceneIdOf(link.getAttribute('href'));
      if (!id) return;
      card.classList.add('srm-card');
      card.appendChild(bar(id));
      ids.push(id);
    });
    const unknown = ids.filter((id) => !state.scenes[id]);
    paint();
    if (unknown.length) refresh(unknown);
  }

  function schedule() {
    if (!pending) pending = setTimeout(scan, 300);
  }

  const css = document.createElement('style');
  css.textContent = `
    .srm-bar { display: inline-flex; gap: 4px; align-items: center; padding: 3px 4px; border: 2px solid var(--srm-color, rgba(255,255,255,.4)); border-radius: 6px; background: rgba(0,0,0,.55); }
    .srm-bar.srm-page { margin: 4px 0 4px 8px; vertical-align: middle; }
    .srm-card { position: relative; }
    /* z-index 0: StashDB's own dropdown lists (react-select menus, z-index 1) open OVER the bars instead of under them (v0.240.20). */
    .srm-card > .srm-bar { position: absolute; top: 6px; left: 6px; z-index: 0; padding: 4px; }
    .srm-chip { display: none; align-items: center; gap: 4px; font-size: 12px; padding: 0 5px; color: inherit; }
    .srm-page .srm-chip { display: inline-flex; }
    .srm-chip-dot { width: 10px; height: 10px; border-radius: 50%; background: rgba(255,255,255,.4); }
    .srm-loading .srm-chip-dot, .srm-loading .srm-dot { animation: srm-pulse 1s ease-in-out infinite; }
    @keyframes srm-pulse { 50% { opacity: .2; } }
    .srm-dot { display: none; width: 14px; height: 14px; border-radius: 50%; background: var(--srm-color, rgba(255,255,255,.4)); }
    /* On a card only the status dot shows; the buttons open while the pointer is on the card. */
    .srm-card > .srm-bar .srm-dot { display: block; }
    .srm-card > .srm-bar .srm-btn { display: none; }
    .srm-card:hover > .srm-bar { padding: 3px 4px; }
    .srm-card:hover > .srm-bar .srm-dot { display: none; }
    .srm-card:hover > .srm-bar .srm-btn { display: inline-flex; }
    .srm-btn { display: inline-flex; align-items: center; justify-content: center; min-width: 28px; height: 28px; padding: 0 5px; border: 0; border-radius: 4px; background: transparent; color: #fff; cursor: pointer; }
    .srm-btn:hover:not(:disabled) { background: rgba(255,255,255,.2); }
    .srm-btn:disabled { opacity: .35; cursor: default; }
    .srm-btn.srm-failed { color: #ff6b6b; }
    .srm-btn.srm-done { color: #5fd38d; }
    .srm-page .srm-btn { color: inherit; }
    .srm-toasts { position: fixed; right: 16px; bottom: 16px; z-index: 2147483647; display: flex; flex-direction: column; gap: 8px; align-items: flex-end; pointer-events: none; }
    .srm-toast { max-width: 360px; padding: 10px 14px; border-radius: 6px; font: 14px/1.35 sans-serif; color: #fff; background: #2b2f36; border-left: 5px solid #5cc98a; box-shadow: 0 4px 14px rgba(0,0,0,.5); }
    .srm-toast.srm-toast-err { border-left-color: #e05c5c; }
  `;
  document.head.appendChild(css);

  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  scan();
})();
