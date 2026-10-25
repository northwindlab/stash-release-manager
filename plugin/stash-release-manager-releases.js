/**
 * StashReleaseManager - Releases page
 * Standalone "browse new releases" page at /plugins/stash-release-manager,
 * modeled loosely on Whisparr's own Calendar view (all watched entities,
 * not scoped to one studio/performer like the entity-panel injection).
 */
(function () {
  'use strict';

  // Per-viewer convenience only (which view mode / which month was open),
  // not synced anywhere -- fine to lose on a hard refresh, unlike actual
  // release data which always comes from the sidecar.
  let viewMode = 'calendar'; // default view (changed 2026-09-12, was 'list')
  let calendarMonth = null; // Date, always normalized to day 1 of its month
  // Calendar filter bar state (2026-09-13, user request -- Stash's own
  // filter UI on the Scenes/Studios pages isn't reusable here at all,
  // this plugin's root is mounted outside Stash's own React tree, so
  // this is a purpose-built equivalent instead: free-text search plus
  // the same Studio/Performer/All filter the Watchlist already has).
  // Kept at module scope, not per-render, so it survives a month change
  // (Prev/Next/Today) instead of resetting every time.
  let calendarSearchText = '';
  // "incl. description" (2026-09-26): also match the scene's description
  // text (release.details), not only title/studio. Off by default so a
  // broad word can't silently widen "Dismiss N shown".
  let calendarSearchDescription = false;
  let calendarSearchTags = false; // 'incl. tags' (2026-10-03, v0.92.0): also match the scene's provider tags
  let calendarTypeFilter = 'both'; // 'both' | 'studio' | 'performer'
  // Provider filter (2026-09-19, user request) -- both checked by
  // default (show everything unless deliberately narrowed, same
  // convention as calendarTypeFilter's 'both' default). Kept as two
  // separate objects (not one shared state) since the List and
  // Calendar views are independent pages a user can filter
  // differently -- same reasoning as calendarSearchText/
  // calendarTypeFilter only ever applying to the Calendar.
  let calendarProviderFilter = { stashdb: true, theporndb: true };
  let listProviderFilter = { stashdb: true, theporndb: true };
  // VR filter (2026-09-22, user request) -- same three-way pattern as
  // calendarTypeFilter/listTypeFilter above ('all' shows everything),
  // against the existing `release.is_vr` flag (set at scan time from
  // StashDB's own "Virtual Reality" scene tag / ThePornDB's site name,
  // see stashdb_client.py/theporndb_client.py -- already used by
  // indexer_search.py's own VR-quality routing, not a new concept).
  // Kept as its own pair, not shared between views, same reasoning as
  // every other filter state above.
  let calendarVrFilter = 'all'; // 'all' | 'vr' | 'non_vr'
  // "Show imported" (v0.129.3): the green pills of scenes this sidecar already imported. Off by default (the list's New releases hides them too);
  // the choice is kept in this browser.
  let calendarShowImported = (() => { try { return localStorage.getItem('srw:calendar-show-imported') === '1'; } catch (e) { return false; } })();
  let listVrFilter = 'all'; // 'all' | 'vr' | 'non_vr'
  // Same text+type filter as the Calendar view's own
  // (calendarSearchText/calendarTypeFilter above) and the Watchlist
  // page's, now on the List view's "New" section too (2026-09-20, user
  // request -- confirmed live this view previously had only the
  // provider checkboxes). Kept as its own pair, not shared with the
  // Calendar's, for the same reason listProviderFilter isn't shared
  // with calendarProviderFilter: List and Calendar are independent
  // pages a user can filter differently.
  let listSearchText = '';
  let listSearchDescription = false; // see calendarSearchDescription
  let listSearchTags = false;
  let listTypeFilter = 'both'; // 'both' | 'studio' | 'performer'
  // Date-range filter (2026-09-20, user request -- the List view's
  // "New" section groups by year first and can grow very long, e.g.
  // 333+ entries across years of StashDB backfill history; a way to
  // narrow to a recent window was missing). Plain ISO date strings
  // ('' means unbounded on that end), matching <input type="date">'s
  // own value format directly -- no parsing needed either direction.
  let listDateFrom = '';
  let listDateUntil = '';
  // Last bulk dismiss (2026-09-24, see buildBulkDismissSlot()) -- kept
  // at module level, not in the slot's own closure, so its Undo
  // survives the full view re-render the dismiss itself triggers.
  // Null once undone/closed.
  let lastBulkDismiss = null; // { scenes: [{provider, scene_stashdb_id}], count }
  // Saved filter presets (2026-09-24, see settings.py's filter_presets)
  // -- shared by List and Calendar, refreshed from settings on every
  // full render of either view.
  let filterPresets = [];

  let scanPollTimer = null;
  // Download-progress poll (2026-09-20, "Activity display" request --
  // no Whisparr-style Activity page exists here, and the existing
  // "search visibility" labels only ever show a before/after result,
  // never a live in-progress percentage). Same start/stop-based-on-
  // whether-there's-anything-to-show pattern as scanPollTimer above,
  // just for GET /releases/download-progress instead of /scan/status.
  // Live push since v0.78.0 (2026-09-26, see core.js's attachLive()):
  // ONE subscription per rendered page (render() below), not one per
  // section build -- the sections get rebuilt by many paths (view switch,
  // actions, calendar), some overlapping, and a subscription tied to a
  // discarded build silently detached itself (found live). It reloads
  // whichever "On Wishlist"/"Imported" sections are on the page right now.
  let wishlistLiveDetach = null;
  let wishlistProgressElements = new Map();
  const liveSectionViews = new Set();

  // A live change (every search run of the sidecar touches rows) rebuilds the list -- which used to close a search panel the user had open
  // ("Search…" / "Last search") together with its results, in the middle of reading them: a view with an open panel is reloaded only once
  // the panel is closed (core.js whenNoOpenPanel).
  function reloadLiveSections() {
    liveSectionViews.forEach((view) => {
      if (view.node.isConnected) window.SRW.whenNoOpenPanel(view.node, () => view.reload());
      else liveSectionViews.delete(view);
    });
  }

  // The page is built behind a "Loading…" (only the header shows) and revealed in one go (v0.175.0): it used to appear piece by piece, first as
  // a plain list and then, after the sections had been turned into tabs, as the calendar with its tabs.
  async function render(container) {
    container.classList.add('srw-page-loading');
    try {
      await renderBody(container);
    } finally {
      container.classList.remove('srw-page-loading');
    }
  }

  async function renderBody(container) {
    const { el } = window.SRW;
    if (scanPollTimer) { clearInterval(scanPollTimer); scanPollTimer = null; }
    if (wishlistLiveDetach) { wishlistLiveDetach(); wishlistLiveDetach = null; }
    container.innerHTML = '';
    // Entries per page of the lists (Settings > Releases page) -- the movie lists use the same number as the scene list.
    try {
      const st = await window.SRW.runOp('settings_get_all');
      window.SRW.listViewPageSize = parseInt(st && st.list_view_page_size, 10) || 100;
      if (st && !st.error) { loadFilterPresets(st); applyDefaultPreset(); }
    } catch (e) { window.SRW.listViewPageSize = 100; }
    const pageHeader = window.SRW.buildPageHeader('releases');
    container.appendChild(pageHeader);
    // First start (v0.179.0): while the entries a new installation needs are missing, the setup guide opens instead of an empty page -- unless
    // "Later" was chosen in this browser; then a reminder under the header says what is missing. A fully set-up installation sees neither.
    try {
      const setup = await window.SRW.runOp('setup_status');
      if (setup && !setup.error && !setup.complete) {
        let later = false;
        try { later = window.localStorage.getItem('srw:setup-later') === '1'; } catch (e) { /* ignore */ }
        if (!later && window.SRW_Settings) { await window.SRW_Settings.renderSetup(container); return; }
        pageHeader.appendChild(el('div', { className: 'srw-setup-banner' }, [
          `Setup is not finished: ${setup.missing.join(', ')} still missing. `,
          el('a', { className: 'srw-btn srw-btn-sm', href: '/plugins/stash-release-manager/setup', textContent: 'Open the setup guide' }),
        ]));
      }
    } catch (e) { /* the reminder is a convenience; the page works without it */ }
    // Pushed by the sidecar (was: polled every 5 s, and only while a
    // download was already listed -- a new grab never showed up without a
    // reload): progress bars update in place, any release change reloads
    // "On Wishlist" and "Imported" (current page kept). Without a live
    // connection the old 5 s progress poll runs instead.
    const liveHint = window.SRW.buildLiveHint();
    container.appendChild(liveHint);
    wishlistLiveDetach = window.SRW.attachLive(liveHint, {
      onProgress: (data) => wishlistProgressElements.forEach((elements, sceneId) => {
        window.SRW.applyProgress(elements, data[sceneId]);
      }),
      onChange: reloadLiveSections,
      onResync: reloadLiveSections,
      poll: () => { if (wishlistProgressElements.size) updateDownloadProgress(wishlistProgressElements); },
      pollMs: 5000,
      hint: liveHint,
    });
    // Settings > Content = movies only: the page is just the movie area (no scene queries at all).
    const scope = await window.SRW.getContentScope();
    if (scope === 'movies') {
      const onlyPane = window.SRW_Films ? window.SRW_Films.buildPane() : null;
      container.appendChild(buildScanRow(null, () => 'films', container, onlyPane));
      if (onlyPane) container.appendChild(onlyPane);
      return;
    }
    // Must resolve before any release row builds a scene link (sceneUrl()
    // reads this cache synchronously) -- see core.js's own comment.
    await window.SRW.loadMetadataProviderUrls();
    container.appendChild(await buildPendingDecisionsBanner(container));

    const listModeBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'List' });
    const calModeBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Calendar' });
    const setActiveModeBtn = () => {
      listModeBtn.classList.toggle('srw-btn-primary', viewMode === 'list');
      calModeBtn.classList.toggle('srw-btn-primary', viewMode === 'calendar');
    };
    setActiveModeBtn();
    listModeBtn.addEventListener('click', async () => {
      viewMode = 'list';
      setActiveModeBtn();
      await renderCurrentView(container);
    });
    calModeBtn.addEventListener('click', async () => {
      viewMode = 'calendar';
      setActiveModeBtn();
      await renderCurrentView(container);
    });

    // View switch = navigation (second level, like Stash's pills), not a filter and not an action button.
    // Not placed here: tabifyReleaseSections() puts it on top of the "New releases" pane (it only applies to that pane).
    viewSwitchRow = el('div', { className: 'srw-subtabs srw-subtabs-inner' }, [listModeBtn, calModeBtn]);

    // Shows whether a scan is running RIGHT NOW regardless of what
    // triggered it -- the "Run scan now" button above only ever
    // reflects a scan *this click* started, so a scan the scheduled
    // background loop kicked off on its own (main.py's _scan_loop(),
    // interval-driven) was previously invisible anywhere in the UI --
    // user report 2026-09-17: "ich würde mir wünschen, dass dieser
    // scan auch irgendwo sichtbar wäre". Polls GET /scan/status (the
    // same endpoint set_scan_progress() in main.py already keeps
    // live), starting/stopping its own interval based on the current
    // phase rather than running one unconditionally forever.
    const scanProgressWrap = el('div', { className: 'srw-scan-progress' });
    container.appendChild(scanProgressWrap);
    checkScanStatus(scanProgressWrap);

    const listWrap = el('div', { className: 'srw-releases-page-list' });
    container.appendChild(listWrap);
    await renderCurrentView(container);

    // Scenes | Films (v0.108.0): the page above becomes the "Scenes" part unchanged, the film wishlist is its own part. The row of
    // tabs is NOT given the section-tabs class (jumpToWishlistRow() counts those).
    const scenesPane = el('div', { className: 'srw-releases-scenes-pane' });
    [...container.children].filter((c) => c !== container.firstElementChild && c !== liveHint).forEach((c) => scenesPane.appendChild(c));
    const filmsPane = el('div', { className: 'srw-releases-films-pane', hidden: true });
    const filmsRoot = window.SRW_Films && scope === 'both' ? window.SRW_Films.buildPane() : null;
    if (filmsRoot) filmsPane.appendChild(filmsRoot);
    // Settings > Content = scenes only: no tabs, just the scene area (the movie area is not even built).
    if (scope === 'scenes') {
      scenesPane.hidden = false;
      container.append(buildScanRow(null, () => 'scenes', container, null), scenesPane);
      return;
    }
    const areaTabs = window.SRW.buildPaneTabs([
      { key: 'scenes', label: 'Scenes', pane: scenesPane },
      { key: 'films', label: 'Movies', pane: filmsPane },
    ], 'srw:releases-area');
    areaTabs.classList.remove('srw-section-tabs');
    areaTabs.classList.add('srw-page-tabs');
    container.append(buildScanRow(areaTabs, () => (filmsPane.hidden ? 'scenes' : 'films'), container, filmsRoot), scenesPane, filmsPane);
  }

  // ONE "Run scan now" for both parts, next to the Scenes | Movies tabs; it runs the part that is selected: the scene scan (watchlist
  // -> StashDB / ThePornDB releases) or the movie feed (new movies of the watched ThePornDB studios -> suggestions). Everything else that
  // used to sit in this row runs by itself (import check hourly, Whisparr registration of new movies) or lives in Maintenance / Settings.
  function buildScanRow(tabs, currentArea, container, filmsRoot) {
    const { el, notify } = window.SRW;
    const btn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Run scan now' });
    const tip = () => (currentArea() === 'films'
      ? 'Runs the movie feed now (otherwise on the schedule from Settings): ONE ThePornDB request; new movies of the watched studios arrive as suggestions on Movies > New releases.'
      : 'Runs the scene scan now (otherwise on the schedule from Settings): looks up new releases of the watched performers and studios on StashDB / ThePornDB and puts them on this page.');
    btn.addEventListener('mouseenter', () => { btn.title = tip(); });
    btn.title = tip();
    btn.addEventListener('click', async () => {
      const area = currentArea();
      btn.disabled = true;
      btn.textContent = 'Scanning...';
      try {
        if (area === 'films') {
          const r = await window.SRW.runOp('films_feed_run');
          if (r.error) notify('Movie scan', r.error, 'error');
          else if (r.skipped) notify('Movie scan', r.reason, 'info');
          else if (r.baseline) notify('Movie scan', 'First run: the starting point was set, nothing is suggested yet.', 'info');
          else notify('Movie scan', `${r.added} new movie(s) suggested (${r.seen} seen in the feed).`, r.added ? 'success' : 'info');
          if (filmsRoot && filmsRoot.reload) await filmsRoot.reload();
        } else {
          await window.SRW.runOp('scan_run');
          await renderCurrentView(container);
        }
      } finally {
        btn.disabled = false;
        btn.textContent = 'Run scan now';
      }
    });
    return el('div', { className: 'srw-scan-row' }, [tabs, btn].filter(Boolean));
  }

  // Instant favorite-change detection (2026-09-21) -- surfaces
  // whatever `pending_favorite_decision` rows the favorite-changed
  // hook flagged (see stash-release-manager.yml's `hooks:` section and
  // api/routers/watchlist.py's favorite_changed()/pending-decisions
  // endpoints) as a dismissible banner, one per (provider, entity),
  // right below the page header -- this is the first thing a user
  // sees on their next visit after unfavoriting something with
  // still-open releases, not a live popup (a Stash hook runs
  // server-side, it can't push into an already-open browser tab, see
  // the design discussion this was built from).
  async function buildPendingDecisionsBanner(container) {
    const { el, confirmInline } = window.SRW;
    const wrap = el('div');
    let decisions = [];
    try {
      const result = await window.SRW.runOp('watchlist_pending_decisions');
      if (Array.isArray(result)) decisions = result;
    } catch (e) { /* best-effort, same as the rest of this page's fetches */ }

    decisions.forEach((d) => {
      const row = el('div', { className: 'srw-pending-decision' }, [
        el('span', {
          className: 'srw-pending-decision-text',
          // Neutral wording since 2026-09-26: removal can be an unfavorite
          // or a removed watchlist tag, depending on watchlist_source.
          textContent: `"${d.name}" was removed from the watchlist with ${d.open_release_count} open release`
            + `${d.open_release_count === 1 ? '' : 's'} (${d.provider}). Keep them, or dismiss them?`,
        }),
        el('button', {
          className: 'srw-btn', textContent: 'Keep',
          onClick: async (e) => {
            e.target.disabled = true;
            await window.SRW.runOp('watchlist_resolve_pending_decision', {
              provider: d.provider, entity_type: d.entity_type, stashdb_id: d.stashdb_id, action: 'keep',
            });
            row.remove();
          },
        }),
        el('button', {
          className: 'srw-btn srw-btn-danger', textContent: 'Dismiss open releases',
          onClick: (e) => {
            confirmInline(e.currentTarget, `Dismiss all ${d.open_release_count} open release(s) for "${d.name}"?`, async () => {
              const res = await window.SRW.runOp('watchlist_resolve_pending_decision', {
                provider: d.provider, entity_type: d.entity_type, stashdb_id: d.stashdb_id, action: 'dismiss',
              });
              row.remove();
              if (res && res.error) { window.SRW.notify('Dismiss', `Dismiss failed: ${res.error}`, 'error'); return; }
              window.SRW.dismissedNotice(`Dismissed ${(res.previous || []).length} open release row(s) of "${d.name}"`, '',
                () => window.SRW.runOp('watchlist_reopen_pending_decision', {
                  provider: d.provider, entity_type: d.entity_type, stashdb_id: d.stashdb_id, name: d.name,
                  open_release_count: d.open_release_count, rows: res.previous || [],
                }), () => render(container));
            }, { danger: true });
          },
        }),
      ]);
      wrap.appendChild(row);
    });
    return wrap;
  }

  function renderScanProgress(scanProgressWrap, status) {
    const { el } = window.SRW;
    scanProgressWrap.innerHTML = '';
    const p = status.progress || {};
    if (!p.phase || p.phase === 'idle') return;
    let label;
    if (p.phase === 'syncing') {
      label = 'Syncing watchlist from Favorites...';
    } else if (p.phase === 'scanning') {
      label = `Scanning... ${p.current}/${p.total}${p.entity_name ? ` (${p.entity_name})` : ''}`;
    } else {
      label = p.phase;
    }
    // "syncing" has no current/total (it's a single bulk favorites-sync
    // call, not a per-entity loop) -- 0% rather than a stalled-looking
    // full bar, same "no total yet" handling as an unreachable percent.
    const pct = p.total ? Math.round((p.current / p.total) * 100) : 0;
    scanProgressWrap.appendChild(el('div', { className: 'srw-progress-track' }, [
      el('div', { className: 'srw-progress-fill', style: `width: ${pct}%` }),
    ]));
    scanProgressWrap.appendChild(el('div', { className: 'srw-hint', textContent: label }));
  }

  async function checkScanStatus(scanProgressWrap) {
    let status;
    try {
      status = await window.SRW.runOp('scan_status');
    } catch (e) {
      return;
    }
    renderScanProgress(scanProgressWrap, status);
    const running = status.progress && status.progress.phase && status.progress.phase !== 'idle';
    if (running && !scanPollTimer) {
      scanPollTimer = setInterval(() => checkScanStatus(scanProgressWrap), 3000);
    } else if (!running && scanPollTimer) {
      clearInterval(scanPollTimer);
      scanPollTimer = null;
    }
  }

  // The status sections (On Wishlist / Imported / Dismissed / Added late) used to be four fold-away panels above the list or
  // calendar. They are tabs now (like on the Performer/Studio pages): [New releases] [On Wishlist (n)] [Imported (n)] ...
  // Done after a view has rendered: the sections are moved out of the way, the labels mirror each section's own live heading.
  let viewSwitchRow = null;
  const SECTION_ORDER = ['sent_to_whisparr', 'imported', 'dismissed', 'late'];
  function tabifyReleaseSections(container) {
    const { el } = window.SRW;
    const listWrap = container.querySelector('.srw-releases-page-list');
    if (!listWrap || listWrap.querySelector(':scope > .srw-section-tabs')) return;
    const groups = [...listWrap.children].filter((c) => c.tagName === 'DETAILS' && c.dataset.status)
      .sort((a, b) => SECTION_ORDER.indexOf(a.dataset.status) - SECTION_ORDER.indexOf(b.dataset.status));
    if (!groups.length) return;
    const mainPane = el('div', { className: 'srw-releases-main-pane' });
    [...listWrap.children].filter((c) => !groups.includes(c)).forEach((c) => mainPane.appendChild(c));
    if (viewSwitchRow) mainPane.prepend(viewSwitchRow);
    // Short tab names (the long headings stay on the sections themselves); the number is mirrored from the live heading.
    const SHORT = { sent_to_whisparr: 'On Wishlist', imported: 'Imported', dismissed: 'Dismissed', late: 'Added late' };
    const items = [{ key: 'new', label: 'New releases', pane: mainPane }];
    groups.forEach((g) => {
      g.open = true;
      g.classList.add('srw-panel-flat');
      items.push({ key: g.dataset.status, label: () => window.SRW.sectionTabLabel(g, SHORT[g.dataset.status] || g.dataset.status),
        pane: g, watch: g.querySelector('summary') });
    });
    const bar = window.SRW.buildPaneTabs(items, 'srw:releases-section', 'new');
    listWrap.append(bar, mainPane, ...groups);
  }

  // The four status sections (On Wishlist / Imported / Dismissed / Added late) are asked for at the same time (v0.175.0: one after the other
  // they added up to the longest wait of the page); returned in display order.
  function buildStatusSections(container) {
    return Promise.all([
      buildStatusSection('sent_to_whisparr', window.SRW.SENT_STATUS_LABEL_LONG, container),
      buildStatusSection('imported', 'Imported', container),
      buildStatusSection('dismissed', 'Dismissed', container),
      buildStatusSection('late', 'Added late (back catalogue)', container),
    ]);
  }

  async function renderCurrentView(container) {
    // Hidden until the view is complete: the status sections used to show up one by one as open lists and only turned into tabs at the end
    // ("first the list appears, then the view switches", v0.175.0). A short note stands in meanwhile.
    const area = container.querySelector('.srw-releases-page-list');
    const note = window.SRW.el('p', { className: 'srw-hint srw-loading-note', textContent: 'Loading…' });
    if (area) { area.hidden = true; area.before(note); }
    try {
      if (viewMode === 'calendar') {
        await renderCalendarView(container);
      } else {
        await renderReleaseList(container);
      }
    } finally {
      if (area) { area.hidden = false; }
      note.remove();
    }
  }

  // Centralized in core.js (2026-09-21, Metadata Provider
  // generalization) -- a release's `provider` can now be any configured
  // stash-box provider, not just 'stashdb'/'theporndb'. Aliased to a
  // bare local name so every existing `sceneUrl(...)` call site here
  // needed no change.
  const sceneUrl = window.SRW.sceneUrl;

  // Centralized in core.js (2026-09-21) -- see its own comment.
  const groupByScene = window.SRW.groupByScene;
  const buildEntityIcons = window.SRW.buildEntityIcons;

  // Shared by both the List and Calendar filter bars (2026-09-19, user
  // request -- motivated by ThePornDB-sourced releases being unable to
  // hand off to Whisparr at all, see handoff.py, so being able to
  // filter them out of either view is genuinely useful, not just
  // cosmetic). `state` is mutated in place (one of
  // calendarProviderFilter/listProviderFilter above) so the caller's
  // own closure sees the change without any extra plumbing.
  function buildProviderCheckboxes(state, onFilterChange, keys = null) {
    const { el } = window.SRW;
    return el('span', { className: 'srw-provider-filter' }, (keys || window.SRW.providerKeys()).map((key) => {
      const checkbox = el('input', { type: 'checkbox' });
      checkbox.dataset.provider = key;
      checkbox.checked = window.SRW.providerOn(state, key);
      checkbox.addEventListener('change', () => { state[key] = checkbox.checked; onFilterChange(); });
      return el('label', { className: 'srw-provider-filter-item' }, [checkbox, ' ' + window.SRW.providerLabel(key)]);
    }));
  }

  // Sets a provider-checkbox span (from buildProviderCheckboxes) to
  // match its state object again, after a preset changed that state.
  function syncProviderCheckboxes(span, state) {
    span.querySelectorAll('input[type=checkbox]').forEach((cb) => {
      cb.checked = window.SRW.providerOn(state, cb.dataset.provider);
    });
  }

  function loadFilterPresets(settings) {
    try {
      const parsed = JSON.parse(settings.filter_presets || '[]');
      filterPresets = Array.isArray(parsed) ? parsed : [];
    } catch (e) {
      filterPresets = [];
    }
  }

  // The default view (v0.217.0): applied once per page load, before the List / Calendar filter bars are built, to the state both views keep at module
  // level. Nothing happens without a preset marked as default.
  let defaultPresetApplied = false;
  function applyDefaultPreset() {
    if (defaultPresetApplied) return;
    defaultPresetApplied = true;
    const p = filterPresets.find((x) => x.default);
    if (!p) return;
    listSearchText = p.search || '';
    listSearchDescription = Boolean(p.description);
    listSearchTags = Boolean(p.tags);
    listTypeFilter = p.type || 'both';
    listVrFilter = p.vr || 'all';
    Object.assign(listProviderFilter, { stashdb: true, theporndb: true }, p.providers || {});
    listDateFrom = p.date_from || '';
    listDateUntil = p.date_until || '';
    sceneFilterState.search = p.search || '';
    sceneFilterState.description = Boolean(p.description);
    sceneFilterState.tags = Boolean(p.tags);
    sceneFilterState.type = p.type || 'both';
    sceneFilterState.vr = p.vr || 'all';
    Object.assign(sceneFilterState.providers, { stashdb: true, theporndb: true }, p.providers || {});
  }

  // Filter presets dropdown (2026-09-24, user request -- pick "PMV"
  // instead of retyping it; pairs with "Dismiss N shown" below the
  // bar). `getState()` returns the bar's current filter as a preset
  // object (minus name), `applyState(preset)` pushes one back into the
  // bar's state + controls and re-filters. Saving under an existing
  // name overwrites it. No prompt()/confirm() -- inline inputs only,
  // see confirmInline()'s own note on blocking modals.
  function buildPresetControl(getState, applyState) {
    const { el } = window.SRW;
    const wrap = el('span', { className: 'srw-filter-presets' });
    const select = el('select', { className: 'srw-filter-preset-select' });
    const deleteBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: '×', title: 'Delete selected preset' });
    const saveBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Save preset' });
    // "Default view" (v0.217.0): the preset marked `default: true` is applied when the Releases page opens (applyDefaultPreset). Kept inside the
    // preset list itself, so no new setting is needed.
    const defaultBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: '☆' });

    function fillSelect(selectedName) {
      select.innerHTML = '';
      select.appendChild(el('option', { value: '', textContent: filterPresets.length ? 'Presets…' : 'No presets' }));
      filterPresets.forEach((p) => select.appendChild(el('option', { value: p.name, textContent: p.default ? `★ ${p.name}` : p.name })));
      select.value = selectedName || '';
      deleteBtn.disabled = !select.value;
      syncDefaultBtn();
    }
    function syncDefaultBtn() {
      const p = filterPresets.find((x) => x.name === select.value);
      defaultBtn.disabled = !p;
      defaultBtn.textContent = p && p.default ? '★' : '☆';
      defaultBtn.title = !p ? 'Pick a preset first, then make it the view the Releases page opens with'
        : (p.default ? 'This preset is the default view -- click to remove that' : 'Open the Releases page with this preset (default view)');
    }
    async function persist(next, selectedName) {
      const result = await window.SRW.runOp('settings_set', { key: 'filter_presets', value: JSON.stringify(next) });
      if (result.error) {
        window.SRW.notify('Presets', `Saving presets failed: ${result.error}`, 'error');
        return;
      }
      filterPresets = next;
      fillSelect(selectedName);
    }

    defaultBtn.addEventListener('click', async () => {
      const name = select.value;
      const p = filterPresets.find((x) => x.name === name);
      if (!p) return;
      const makeDefault = !p.default;
      await persist(filterPresets.map((x) => ({ ...x, default: makeDefault && x.name === name ? true : undefined })), name);
      window.SRW.notify('Default view', makeDefault ? `The Releases page now opens with “${name}”.` : 'No default view any more -- the Releases page opens unfiltered.', 'success');
    });
    select.addEventListener('change', () => {
      deleteBtn.disabled = !select.value;
      syncDefaultBtn();
      const preset = filterPresets.find((p) => p.name === select.value);
      if (preset) applyState(preset);
    });
    deleteBtn.addEventListener('click', () => {
      const name = select.value;
      if (!name) return;
      window.SRW.confirmInline(deleteBtn, `Delete preset "${name}"?`, async () => {
        await persist(filterPresets.filter((p) => p.name !== name), '');
      }, { danger: true });
    });
    saveBtn.addEventListener('click', () => {
      saveBtn.style.display = 'none';
      const nameInput = el('input', { type: 'text', placeholder: 'Preset name', value: select.value, className: 'srw-filter-preset-name' });
      const close = () => { form.remove(); saveBtn.style.display = ''; };
      const doSave = async () => {
        const name = nameInput.value.trim();
        if (!name) return;
        const next = filterPresets.filter((p) => p.name !== name);
        const old = filterPresets.find((p) => p.name === name);
        next.push({ ...getState(), name, ...(old && old.default ? { default: true } : {}) });
        next.sort((a, b) => a.name.localeCompare(b.name));
        await persist(next, name);
        close();
      };
      nameInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') doSave();
        if (e.key === 'Escape') close();
      });
      const form = el('span', { className: 'srw-inline-confirm' }, [
        nameInput,
        el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Save', onClick: doSave }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Cancel', onClick: close }),
      ]);
      saveBtn.after(form);
      nameInput.focus();
    });

    // The dropdown shows the preset the bar's current filter equals (e.g. the default view just applied), else "Presets…".
    const cur = getState();
    const same = (p) => ['search', 'type', 'vr'].every((k) => (p[k] || (k === 'type' ? 'both' : k === 'vr' ? 'all' : '')) === (cur[k] || (k === 'type' ? 'both' : k === 'vr' ? 'all' : '')))
      && Boolean(p.description) === Boolean(cur.description) && Boolean(p.tags) === Boolean(cur.tags)
      && ['stashdb', 'theporndb'].every((k) => ((p.providers || {})[k] !== false) === ((cur.providers || {})[k] !== false));
    const match = filterPresets.find((p) => same(p) && (!cur.date_from && !cur.date_until ? true : (p.date_from || '') === cur.date_from && (p.date_until || '') === cur.date_until));
    fillSelect(match ? match.name : '');
    wrap.append(select, defaultBtn, deleteBtn, saveBtn);
    return wrap;
  }

  // "Dismiss N shown" (2026-09-24, user request -- e.g. filter for
  // "PMV" and clear every match at once instead of one Dismiss click
  // per entry). Shared by List and Calendar: the caller passes exactly
  // the scenes its current filter shows (already scene-merged, 'new'
  // only) and is re-called on every filter change so the count stays
  // live. Only offered while some filter is actually narrowing the
  // view -- an unfiltered "Dismiss all 800" is one misclick away from
  // wiping the whole list. Reversible: an Undo stays visible after the
  // re-render (see lastBulkDismiss), and every entry also still shows
  // up in the Dismissed section with its own Restore.
  // `opts` (2026-09-26, List view paging): {count, loadScenes} instead
  // of `targets` -- the List view only has the current page loaded, so
  // it passes the server's total match count and a loader for ALL
  // matching scenes, called only once the user confirms.
  // One entry per SCENE (v0.216.1): a scene that came in through two watched entities (studio + performer) has two release rows, but is one
  // dismiss -- the buttons counted the rows ("Dismiss 10 on this page" next to "Dismiss all 9 matching").
  function uniqueScenes(list) {
    const seen = new Set();
    return list.filter((r) => { const k = `${r.provider}\u0000${r.scene_stashdb_id}`; if (seen.has(k)) return false; seen.add(k); return true; });
  }

  function buildBulkDismissSlot(targets, filterActive, container, opts = null) {
    const { el } = window.SRW;
    targets = uniqueScenes(targets);
    if (opts && opts.pageScenes) opts = { ...opts, pageScenes: uniqueScenes(opts.pageScenes) };
    const children = [];
    if (lastBulkDismiss) {
      const { scenes, count } = lastBulkDismiss;
      const undoParts = [];
      undoParts.push(el('span', { textContent: `Dismissed ${count}.` }));
      undoParts.push(el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Undo',
        onClick: async (e) => {
          const undoBtn = e.currentTarget; // null again after the await below
          undoBtn.disabled = true;
          const result = await window.SRW.runOp('releases_bulk_status', { action: 'undismiss', scenes });
          if (result.error) {
            window.SRW.notify('Undo', `Undo failed: ${result.error}`, 'error');
            undoBtn.disabled = false;
            return;
          }
          lastBulkDismiss = null;
          await renderCurrentView(container);
        },
      }));
      undoParts.push(el('button', {
        className: 'srw-btn srw-btn-sm', textContent: '×', title: 'Hide',
        onClick: (e) => { lastBulkDismiss = null; e.currentTarget.parentNode.remove(); },
      }));
      children.push(el('span', { className: 'srw-bulk-dismiss-note' }, undoParts));
    }
    const count = opts ? opts.count : targets.length;
    if (filterActive && count) {
      const btn = el('button', {
        className: 'srw-btn srw-btn-sm srw-btn-danger',
        textContent: opts ? `Dismiss all ${count} matching` : `Dismiss ${count} shown`,
      });
      // One shared click handler for both buttons: `getScenes` yields what
      // gets dismissed, `n` is the number shown in the confirmation.
      const wire = (button, n, what, getScenes) => button.addEventListener('click', () => {
        window.SRW.confirmInline(button, (what === 'all' ? `Dismiss all ${n} releases matching the current filter?` : `Dismiss the ${n} releases on this page?`), async () => {
          const scenes = await getScenes();
          if (!Array.isArray(scenes)) {
            window.SRW.notify('Bulk dismiss', `Bulk dismiss failed: ${(scenes && scenes.error) || 'could not load the matching releases'}`, 'error');
            return;
          }
          const result = await window.SRW.runOp('releases_bulk_status', { action: 'dismiss', scenes });
          if (result.error) {
            window.SRW.notify('Bulk dismiss', `Bulk dismiss failed: ${result.error}`, 'error');
            return;
          }
          lastBulkDismiss = { scenes, count: scenes.length };
          await renderCurrentView(container);
        }, { danger: true });
      });
      if (opts && opts.pageScenes && opts.pageScenes.length) {
        const pageBtn = el('button', {
          className: 'srw-btn srw-btn-sm srw-btn-danger',
          textContent: `Dismiss ${opts.pageScenes.length} on this page`,
        });
        wire(pageBtn, opts.pageScenes.length, 'the', async () => opts.pageScenes);
        children.push(pageBtn);
      }
      wire(btn, count, 'all', async () => (opts
        ? opts.loadScenes()
        : targets.map((r) => ({ provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }))));
      children.push(btn);
    }
    return el('div', { className: 'srw-bulk-dismiss' }, children);
  }

  function buildListFilterBar(onFilterChange) {
    const { el } = window.SRW;
    const searchInput = el('input', {
      type: 'search', placeholder: 'Search title or studio…', value: listSearchText,
      className: 'srw-search-input',
      oninput: (e) => { listSearchText = e.target.value; onFilterChange(); },
    });
    const descCheckbox = el('input', { type: 'checkbox' });
    descCheckbox.checked = listSearchDescription;
    descCheckbox.addEventListener('change', () => { listSearchDescription = descCheckbox.checked; onFilterChange(); });
    const descToggle = el('label', {
      className: 'srw-provider-filter-item', title: 'Also match the scene description, not only title/studio',
    }, [descCheckbox, ' incl. description']);
    const tagsCheckbox = el('input', { type: 'checkbox' });
    tagsCheckbox.checked = listSearchTags;
    tagsCheckbox.addEventListener('change', () => { listSearchTags = tagsCheckbox.checked; onFilterChange(); });
    const tagsToggle = el('label', {
      className: 'srw-provider-filter-item', title: 'Also match the scene\'s tags, not only title/studio',
    }, [tagsCheckbox, ' incl. tags']);
    // Drop-downs like the other lists' filter bars (v0.225.5); the function names stay for the presets that set the values.
    const typeSelect = window.SRW.buildSelect(
      [{ key: 'both', label: 'All types' }, { key: 'studio', label: 'Studios' }, { key: 'performer', label: 'Performer' }],
      () => listTypeFilter, (v) => { listTypeFilter = v; onFilterChange(); }, 'Came in through a watched studio or a watched performer');
    function updateActiveFilterBtn() { typeSelect.value = listTypeFilter; }
    const vrSelect = window.SRW.buildSelect(
      [{ key: 'all', label: 'VR + Non-VR' }, { key: 'vr', label: 'VR' }, { key: 'non_vr', label: 'Non-VR' }],
      () => listVrFilter, (v) => { listVrFilter = v; onFilterChange(); }, 'VR or flat releases');
    function updateActiveVrBtn() { vrSelect.value = listVrFilter; }
    // Date-range filter (2026-09-20) -- against `scene_date` (falling
    // back to `scene_created`'s own day, see release_repo.query_releases()),
    // same field the year/month/day grouping below is already built
    // from. Plain <input type="date"> pair, same convention as the
    // Watchlist tab's own per-entity auto-hand-off from/until fields.
    const dateFromInput = el('input', { type: 'date', className: 'srw-list-date-filter', value: listDateFrom });
    const dateUntilInput = el('input', { type: 'date', className: 'srw-list-date-filter', value: listDateUntil });
    dateFromInput.addEventListener('change', () => { listDateFrom = dateFromInput.value; onFilterChange(); });
    dateUntilInput.addEventListener('change', () => { listDateUntil = dateUntilInput.value; onFilterChange(); });
    const clearDatesBtn = el('button', {
      className: 'srw-btn srw-btn-sm', textContent: '×', title: 'Clear date filter',
      onClick: () => {
        listDateFrom = ''; listDateUntil = '';
        dateFromInput.value = ''; dateUntilInput.value = '';
        onFilterChange();
      },
    });
    const dateRangeWrap = el('span', { className: 'srw-list-date-range' }, [
      el('span', { className: 'srw-list-date-range-label', textContent: 'from' }),
      dateFromInput,
      el('span', { className: 'srw-list-date-range-label', textContent: 'until' }),
      dateUntilInput,
      clearDatesBtn,
    ]);
    const providerBoxes = buildProviderCheckboxes(listProviderFilter, onFilterChange);
    const presetControl = buildPresetControl(
      () => ({
        search: listSearchText, description: listSearchDescription, tags: listSearchTags, type: listTypeFilter, vr: listVrFilter,
        providers: { ...listProviderFilter }, date_from: listDateFrom, date_until: listDateUntil,
      }),
      (p) => {
        listSearchText = p.search || '';
        listSearchDescription = Boolean(p.description);
        descCheckbox.checked = listSearchDescription;
        listSearchTags = Boolean(p.tags);
        tagsCheckbox.checked = listSearchTags;
        listTypeFilter = p.type || 'both';
        listVrFilter = p.vr || 'all';
        Object.assign(listProviderFilter, { stashdb: true, theporndb: true }, p.providers || {});
        listDateFrom = p.date_from || '';
        listDateUntil = p.date_until || '';
        searchInput.value = listSearchText;
        dateFromInput.value = listDateFrom;
        dateUntilInput.value = listDateUntil;
        updateActiveFilterBtn();
        updateActiveVrBtn();
        syncProviderCheckboxes(providerBoxes, listProviderFilter);
        onFilterChange();
      },
    );
    return el('div', { className: 'srw-filter-bar' }, [
      presetControl,
      searchInput,
      descToggle,
      tagsToggle,
      typeSelect,
      vrSelect,
      dateRangeWrap,
      providerBoxes,
    ]);
  }

  // Current List-view page (1-based) -- module level so it survives a
  // full re-render (e.g. after a dismiss), reset to 1 on filter changes.
  let listPage = 1;

  // The List view's filter state as releases_query arguments (2026-09-26,
  // server-side filtering/paging -- see release_repo.query_releases()).
  function listQueryArgs() {
    return {
      statuses: ['new'],
      q: listSearchText.trim(),
      search_description: listSearchDescription,
      search_tags: listSearchTags,
      entity_type: listTypeFilter === 'both' ? null : listTypeFilter,
      vr: listVrFilter === 'all' ? null : listVrFilter,
      providers: window.SRW.providersSelected(listProviderFilter),
      date_from: listDateFrom || null,
      date_until: listDateUntil || null,
    };
  }

  function buildServerPager(page, totalPages, total, onPage, perPage = null) {
    return window.SRW.buildPager({ page, totalPages, total, perPage, onPage });
  }

  async function renderReleaseList(container) {
    await renderReleaseListInner(container);
    tabifyReleaseSections(container);
  }

  async function renderReleaseListInner(container) {
    const { el } = window.SRW;
    const listWrap = container.querySelector('.srw-releases-page-list');
    listWrap.textContent = 'Loading...';
    const settings = await window.SRW.runOp('settings_get_all');
    loadFilterPresets(settings);
    // Paged server-side since 2026-09-26 (was: load up to list_view_limit
    // rows and filter in the browser, which silently missed older
    // entries once the backlog grew past that limit).
    const perPage = parseInt(settings.list_view_page_size, 10) || 100;

    listWrap.innerHTML = '';

    // Moved above the year groups AND above the filter bar (2026-09-20).
    // Not affected by the "New" list's own filter bar below.
    (await buildStatusSections(container)).forEach((node) => listWrap.appendChild(node));

    const bulkSlot = el('div');
    const newListWrap = el('div');
    let requestSeq = 0;
    async function loadPage(page) {
      const seq = ++requestSeq;
      const args = listQueryArgs();
      const response = await window.SRW.runOp('releases_query', { ...args, page, per_page: perPage });
      if (seq !== requestSeq) return; // a newer filter change is already on its way
      newListWrap.innerHTML = '';
      bulkSlot.innerHTML = '';
      if (response.error) {
        newListWrap.appendChild(el('p', { textContent: `Error: ${response.error}` }));
        return;
      }
      const totalPages = Math.max(1, Math.ceil(response.total / perPage));
      if (page > totalPages) { listPage = totalPages; loadPage(totalPages); return; }
      listPage = page;
      const filterActive = Boolean(args.q || args.entity_type || args.vr || args.date_from || args.date_until
        || !window.SRW.providersAllOn(listProviderFilter));
      bulkSlot.appendChild(buildBulkDismissSlot([], filterActive, container, {
        count: response.total,
        pageScenes: response.rows.map((r) => ({ provider: r.provider, scene_stashdb_id: r.scene_stashdb_id })),
        loadScenes: async () => {
          const all = await window.SRW.runOp('releases_query', { ...args, ids_only: true });
          return all.error ? all : all.scenes;
        },
      }));
      if (!response.rows.length) {
        newListWrap.appendChild(el('p', {
          textContent: filterActive ? 'No releases match the current filter.' : 'No new releases.',
        }));
        return;
      }
      if (totalPages > 1) newListWrap.appendChild(buildServerPager(page, totalPages, response.total, loadPage, perPage));
      newListWrap.appendChild(buildNewReleasesByDate(response.rows, container));
      if (totalPages > 1) newListWrap.appendChild(buildServerPager(page, totalPages, response.total, loadPage, perPage));
      // Fire-and-forget Whisparr/library badge pass, capped to the newest
      // 50 rows of this page (see annotateWhisparrStatus()).
      annotateWhisparrStatus(newListWrap, response.rows.slice(0, 50), container);
    }
    // Filter changes go back to page 1; typing is debounced so a query
    // isn't sent per keystroke.
    let debounce = null;
    function onFilterChange() {
      clearTimeout(debounce);
      debounce = setTimeout(() => loadPage(1), 300);
    }
    listWrap.appendChild(buildListFilterBar(onFilterChange));
    listWrap.appendChild(bulkSlot);
    listWrap.appendChild(newListWrap);
    await loadPage(listPage);
  }


  // One flat list per page, newest first (v0.102.1) -- the Year > Month > Day folding is gone: the list pages server-side and
  // the filter bar's date range narrows it. Scene merge first, so a scene found via several watched entities is one row.
  function buildNewReleasesByDate(releases, container) {
    const merged = groupByScene(releases);
    const day = (r) => r.scene_date || r.scene_created.slice(0, 10);
    merged.sort((a, b) => day(b).localeCompare(day(a)));
    return buildDayReleaseList(merged, container, true);
  }

  // Centralized in core.js (2026-09-21) -- see its own comment.
  const buildLocalEntityLink = window.SRW.buildLocalEntityLink;
  const buildReleaseThumb = window.SRW.buildReleaseThumb;
  const buildStudioContext = window.SRW.buildStudioContext;

  // arrangeReleaseRow / rowMeta live in core.js now (shared with the Performer/Studio Releases tab).
  const arrangeReleaseRow = window.SRW.arrangeReleaseRow;
  const rowMeta = window.SRW.rowMeta;

  function buildDayReleaseList(dayReleases, container, showDate = false) {
    const { el } = window.SRW;
    const ul = el('ul', { className: 'srw-release-list' });
    dayReleases.forEach((r) => {
      const isTpdb = r.provider !== 'stashdb'; // any provider but StashDB: handled by this sidecar without Whisparr
      const items = [
        buildReleaseThumb(r),
        buildEntityIcons(r.entity_types),
        buildLocalEntityLink(r),
        buildStudioContext(r),
        el('a', {
          className: 'srw-release-title', href: sceneUrl(r.provider, r.scene_stashdb_id),
          target: '_blank', rel: 'noopener noreferrer',
          textContent: r.title || '(untitled)',
        }),
      ];
      // In the flat list the row carries its own date (there is no day heading above it any more).
      if (showDate) items.push(el('span', { className: 'srw-release-date', textContent: r.scene_date || r.scene_created.slice(0, 10) }));
      // ThePornDB releases can be added too since 2026-09-25 -- they
      // take the Whisparr-free path (handoff.hand_off()); the badge just
      // shows where the release was found.
      if (isTpdb) {
        items.push(el('span', {
          className: 'srw-release-source',
          title: `Found via ${window.SRW.providerLabel(r.provider)} -- handled by this sidecar without Whisparr`,
          textContent: window.SRW.providerLabel(r.provider),
        }));
      }
      {
        // Defaults to "Add to Wishlist" (primary blue) -- annotateWhisparrStatus()
        // patches this to "Search now" (teal) or removes it in favor of
        // a badge, same as the Studio/Performer panel's
        // buildReleaseList()/annotateLibraryStatus() (2026-09-13).
        // Labels renamed from "Send to Whisparr"/"Search in Whisparr"
        // 2026-09-19 -- see entity-panel.js's confirmHandoff() rename
        // note (the actual search runs in this sidecar now, not
        // Whisparr).
        items.push(window.SRW.buildAddOnlyButton(r, () => renderReleaseList(container)));
        items.push(el('button', {
          className: 'srw-btn srw-btn-primary srw-whisparr-action',
          textContent: 'Add + search',
          title: 'Put it on the wishlist and search the indexers right now',
          onClick: async (e) => {
            const doHandoff = async () => {
              const result = await window.SRW.runOp('releases_handoff', {
                provider: r.provider, entity_type: r.entity_type,
                entity_stashdb_id: r.entity_stashdb_id, scene_stashdb_id: r.scene_stashdb_id,
              });
              if (result.error) { window.SRW.notify(`Adding failed: ${r.title || ''}`, result.error, 'error'); return; }
              window.SRW.notifyHandoff(r.title, result);
              await renderReleaseList(container);
            };
            // "Search now" fires immediately, no confirm() -- see
            // entity-panel.js's confirmHandoff() note (2026-09-19).
            if (e.currentTarget.textContent !== 'Search now') {
              const title = r.title || r.scene_stashdb_id;
              window.SRW.confirmInline(e.currentTarget, `Add "${title}" to your wishlist and search for it?`, doHandoff);
              return;
            }
            await doHandoff();
          },
        }));
      }
      items.push(el('button', {
        className: 'srw-btn',
        textContent: 'Dismiss',
        onClick: async () => {
          const res = await window.SRW.dismissRelease(r);
          window.SRW.releaseDismissedNotice(r, res, () => renderReleaseList(container));
          await renderReleaseList(container);
        },
      }));
      // Hide the scene's studio (2026-10-03): for a sub-studio of a watched network studio the user does not want. Only releases that
      // came in through a STUDIO are hidden (performer hits stay); Settings > Hidden studios brings them back.
      if (((r.entity_types || []).includes('studio') || r.entity_type === 'studio') && !/^(upgrade|film):/.test(String(r.entity_stashdb_id || ''))) {
        const hideBtn = el('button', {
          className: 'srw-btn', textContent: 'Hide studio',
          title: `Hide every release of “${r.studio_name || 'this studio'}” that came in through a watched studio`,
        });
        hideBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
          `Hide the studio “${r.studio_name || '?'}”? Its releases that came in through a watched studio leave the Calendar and List (nothing is deleted; Settings > Hidden studios shows them again).`,
          async () => {
            const result = await window.SRW.runOp('muted_studios_mute', { provider: r.provider, scene_id: r.scene_stashdb_id });
            if (result.error) { window.SRW.notify('Hide studio', result.error, 'error'); return; }
            window.SRW.dismissedNotice('Studio hidden', `“${result.name || r.studio_name}” hidden (${result.hidden} release(s)).`,
              () => window.SRW.runOp('muted_studios_unmute', { studio_id: result.studio_id }), () => renderReleaseList(container));
            await renderReleaseList(container);
          }));
        items.push(hideBtn);
      }
      // Same amber accent as the Calendar's ThePornDB pills (2026-09-12
      // follow-up -- user noticed the List view and the Studio/Performer
      // entity panel still looked uncolored after that change, since
      // this is entirely separate markup from the Calendar's pills).
      const itemClass = `srw-release-item srw-release-item-${r.provider}`; // tint from the provider registry colour
      const li = el('li', { className: itemClass }, items);
      li.dataset.sceneId = r.scene_stashdb_id;
      arrangeReleaseRow(li, r);
      ul.appendChild(li);
    });
    return ul;
  }

  // Mirrors the Studio/Performer panel's annotateLibraryStatus() (see
  // stash-release-manager-entity-panel.js) -- kept as a separate copy
  // rather than a shared helper since this page's list/badge DOM is
  // built independently, but the same batched
  // releases_library_status op and the same three outcomes apply:
  // hide (satisfied), relabel to "Search now" (tracked in
  // Whisparr, no file), or replace with a "Downloaded" badge (tracked
  // and has a file, Stash just hasn't rescanned yet). Fire-and-forget,
  // best-effort -- the list itself already works without this.
  async function annotateWhisparrStatus(listWrap, releases, container) {
    const scenes = releases.filter((r) => r.provider === 'stashdb')
      .map((r) => ({ scene_stashdb_id: r.scene_stashdb_id, is_vr: !!r.is_vr }));
    if (!scenes.length) return;
    try {
      const status = await window.SRW.runOp('releases_library_status', { scenes });
      if (status.error) return;
      const { el } = window.SRW;
      Object.entries(status).forEach(([sceneId, info]) => {
        const li = listWrap.querySelector(`li[data-scene-id="${CSS.escape(sceneId)}"]`);
        if (!li) return;
        if (info.satisfied) { li.remove(); return; }
        const actionBtn = li.querySelector('.srw-whisparr-action');
        if (info.whisparr_exists || info.whisparr_has_file || info.whisparr_file_mismatch) li.querySelectorAll('.srw-whisparr-add-only').forEach((x) => x.remove());
        if (info.in_library) {
          const label = info.quality ? `In Library — ${info.quality}` : 'In Library';
          const badge = el('span', { className: 'srw-release-in-library', textContent: label });
          rowMeta(li).appendChild(badge);
          window.SRW.addOpenSceneLink(li, 'stashdb', sceneId); // next to Dismiss
        }
        if (info.whisparr_file_mismatch) {
          // Mirrors the Studio/Performer panel's own handling (see
          // stash-release-manager-entity-panel.js's
          // annotateLibraryStatus() for the full reasoning) -- checked
          // before whisparr_has_file below since a mismatch also has
          // Whisparr's own hasFile true.
          const r = releases.find((x) => x.scene_stashdb_id === sceneId);
          rowMeta(li).appendChild(window.SRW.mismatchWarning(info));
          if (actionBtn && r) {
            const fixBtn = actionBtn.cloneNode(false);
            fixBtn.textContent = 'Fix wrong download';
            fixBtn.className = 'srw-btn srw-btn-danger';
            fixBtn.addEventListener('click', () => {
              const title = r.title || r.scene_stashdb_id;
              window.SRW.confirmInline(fixBtn,
                `Whisparr's file for "${title}" doesn't match this scene -- delete the wrong file from `
                + `Whisparr (and disk) and have it search for a correct release instead?`,
                async () => {
                  const result = await window.SRW.runOp('releases_fix_mismatch', {
                    provider: r.provider, entity_type: r.entity_type,
                    entity_stashdb_id: r.entity_stashdb_id, scene_stashdb_id: r.scene_stashdb_id,
                  });
                  if (result.error) { window.SRW.notify('Fix wrong download', `Fix failed: ${result.error}`, 'error'); return; }
                  await renderReleaseList(container);
                },
                { danger: true },
              );
            });
            actionBtn.replaceWith(fixBtn);
          }
        } else if (info.whisparr_has_file) {
          if (actionBtn) actionBtn.remove();
          rowMeta(li).appendChild(el('span', {
            className: 'srw-release-whisparr-file',
            textContent: info.in_library ? 'Whisparr holds this file too -- the library copy is below the quality profile\'s cutoff'
              : 'Downloaded (Whisparr) — awaiting Stash rescan',
          }));
        } else if (info.whisparr_exists && actionBtn) {
          actionBtn.textContent = 'Search now';
          actionBtn.classList.remove('srw-btn-primary');
          actionBtn.classList.add('srw-btn-whisparr');
        }
      });
      window.SRW.resolveSceneLinks(listWrap);
    } catch (e) {
      // Best-effort, same reasoning as entity-panel.js's annotateLibraryStatus().
    }
  }

  // Status sections (On Wishlist / Imported / Dismissed) -- filtered +
  // paged server-side since 2026-09-26 (was: first 200 rows only, the
  // rest silently cut off). Same filter bar as the List view.
  const STATUS_SECTION_ORDER = { sent_to_whisparr: 'handoff', imported: 'imported', dismissed: 'day' };
  const statusSectionViews = {};

  async function buildStatusSection(status, label, container) {
    const { el } = window.SRW;
    const summary = el('summary', { textContent: `${label} — …` });
    const group = el('details', { className: 'srw-date-group' }, [summary]);
    group.dataset.status = status;
    window.SRW.rememberOpen(group, `releases:status:${status}`, false);
    const order = STATUS_SECTION_ORDER[status] || 'day';
    const sortKey = order === 'handoff' ? 'handoff_at' : (order === 'imported' ? 'imported_at' : null);
    // 'late' = back-catalogue additions (status 'backlog' + late_addition,
    // 2026-09-26) -- otherwise only visible on each entity's page.
    const isLate = status === 'late';
    const view = window.SRW.buildFilteredPagedList({
      filters: ['search', 'description', 'type', 'vr', 'dates', 'providers', ...(status === 'sent_to_whisparr' ? ['upgrades'] : [])],
      perPage: 50,
      showFilterBarFrom: 10,
      emptyText: 'None.',
      fetchPage: async (f, page, perPage) => {
        const response = await window.SRW.runOp('releases_query', {
          ...window.SRW.releaseQueryArgs(f), statuses: [isLate ? 'backlog' : status], order, page, per_page: perPage,
          ...(isLate ? { late_addition: true } : {}),
        });
        if (response.error) return response;
        let items = groupByScene(response.rows);
        if (status === 'sent_to_whisparr') await window.SRW.fillCast(items); // the cast next to the studio on wishlist rows
        if (sortKey) items = items.sort((a, b) => String(b[sortKey] || '').localeCompare(String(a[sortKey] || '')));
        return { total: response.total, items };
      },
      renderItems: (items) => renderStatusItems(items, status, container),
      onTotal: (total, active) => { summary.textContent = `${label} — ${total}${active ? ' (filtered)' : ''}`; },
    });
    statusSectionViews[status] = view;
    if (status === 'sent_to_whisparr' || status === 'imported') liveSectionViews.add(view);
    group.appendChild(view.node);
    await view.reset();
    return group;
  }

  function renderStatusItems(items, status, container) {
    const { el } = window.SRW;
    const ul = el('ul', { className: 'srw-release-list' });
    // scene_stashdb_id -> {fill, text} -- only ever populated in the
    // 'sent_to_whisparr' branch below, read by updateDownloadProgress().
    const progressElements = new Map();
    items.forEach((r) => {
      const when = status === 'imported' ? r.imported_at : r.handoff_at;
      const li = [
        buildReleaseThumb(r),
        buildEntityIcons(r.entity_types),
        buildLocalEntityLink(r),
        buildStudioContext(r),
        el('a', {
          className: 'srw-release-title', href: sceneUrl(r.provider, r.scene_stashdb_id),
          target: '_blank', rel: 'noopener noreferrer',
          textContent: r.title || '(untitled)',
        }),
      ].filter(Boolean);
      // Dismissed has no timestamp field of its own (unlike handoff_at/
      // imported_at) and, unlike those two, is meant to be reversible --
      // added 2026-09-12 after the user asked what Dismiss actually
      // does and found there was previously no way back once clicked.
      if (status === 'late') {
        li.push(el('span', { className: 'srw-release-date', textContent: r.scene_date || '' }));
        li.push(window.SRW.buildLateAdditionBadge(r));
        li.push(window.SRW.buildAddOnlyButton(r, () => renderReleaseList(container)));
        li.push(el('button', {
          className: 'srw-btn srw-btn-primary srw-whisparr-action', textContent: 'Add + search',
          title: 'Put it on the wishlist and search the indexers right now',
          onClick: (e) => window.SRW.confirmInline(e.currentTarget,
            `Add "${r.title || r.scene_stashdb_id}" to your wishlist and search for it?`, async () => {
              const result = await window.SRW.runOp('releases_handoff', {
                provider: r.provider, entity_type: r.entity_type,
                entity_stashdb_id: r.entity_stashdb_id, scene_stashdb_id: r.scene_stashdb_id,
              });
              if (result.error) { window.SRW.notify(`Adding failed: ${r.title || ''}`, result.error, 'error'); return; }
              window.SRW.notifyHandoff(r.title, result);
              await renderReleaseList(container);
            }),
        }));
        li.push(el('button', {
          className: 'srw-btn', textContent: 'Dismiss',
          onClick: async () => {
            const res = await window.SRW.dismissRelease(r);
            window.SRW.releaseDismissedNotice(r, res, () => statusSectionViews.late.reload());
            await statusSectionViews.late.reload();
          },
        }));
      } else if (status === 'dismissed') {
        li.push(window.SRW.iconButton('button', 'faRotateLeft', 'Restore', {
          className: 'srw-btn',
          title: 'Restore: back on the list',
          onClick: async () => {
            await window.SRW.runOp('releases_undismiss', {
              provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
            });
            await renderReleaseList(container);
          },
        }));
      } else {
        if (status === 'imported') { // the downloaded scene in Stash, with the other buttons of the row
          const open = window.SRW.iconButton('a', 'faPlay', 'Open scene', { className: 'srw-btn srw-open-scene', target: '_blank', rel: 'noopener noreferrer', hidden: true,
            title: 'Open this scene in Stash (new tab)' });
          open.dataset.provider = r.provider; open.dataset.scene = r.scene_stashdb_id;
          li.push(open);
        }
        li.push(el('span', { className: 'srw-release-date', title: status === 'imported' ? 'Imported' : 'Added to the wishlist',
          textContent: when ? `${status === 'imported' ? 'imported' : 'added'} ${when.slice(0, 16).replace('T', ' ')}` : '' }));
      }
      // Milestone 3 (2026-09-19): manual per-release fallback to
      // Whisparr's own automatic search+grab, for a release this
      // project's own indexer search hasn't found anything acceptable
      // for yet -- only makes sense once already sent (Whisparr needs
      // to already be tracking the movie, see handoff.py's
      // fallback_to_whisparr()).
      if (status === 'sent_to_whisparr') {
        // "Search visibility" (2026-09-19, user's own request right
        // after the first real grab succeeded -- no Whisparr-style
        // Activity page exists here). `last_search_result`/
        // `last_search_at` are set by indexer_search.search_and_grab()
        // on every attempt (see release_repo.set_search_result_for_scene()),
        // not just a successful one.
        li.push(el('span', {
          className: 'srw-search-status',
          title: r.last_search_at ? `Last checked: ${r.last_search_at.slice(0, 16).replace('T', ' ')}` : '',
          textContent: window.SRW.searchStatusLabel(r.download_job_id, r.last_search_result),
        }));
        // Backend-aware (2026-09-25, stage 3) -- see core.js's
        // buildSearchActions(): Whisparr fallback only with Whisparr
        // configured, own-query search over Prowlarr/direct indexers.
        li.push(window.SRW.buildSearchActions(r, () => renderCurrentView(container)));
        if (!r.download_job_id) li.push(window.SRW.buildWishlistDismiss(r, () => renderCurrentView(container)));
        // Live download progress (2026-09-20, "Activity display"
        // request) -- only meaningful once something's actually been
        // grabbed (`download_job_id` set); a release still searching
        // has nothing to show here. Registered in `progressElements`
        // so the poll below (see updateDownloadProgress()) can update
        // these specific DOM nodes in place without rebuilding the row
        // -- same "patch, don't rebuild" reasoning already used
        // elsewhere on this page (searchStatusLabel(), etc.).
        if (r.download_job_id) {
          const fill = el('div', { className: 'srw-download-progress-fill' });
          const text = el('span', { className: 'srw-download-progress-text' });
          const wrap = el('span', { className: 'srw-download-progress' }, [
            el('div', { className: 'srw-download-progress-track' }, [fill]),
            text,
          ]);
          li.push(wrap);
          progressElements.set(r.scene_stashdb_id, { fill, text, wrap });
        }
        // RESULT_NEEDS_REVIEW (2026-09-19): a candidate matched every
        // one of Whisparr's own credited performers for this movie but
        // didn't clear the bar for an automatic grab (see
        // indexer_search.py's _all_performers_plausible()/
        // _select_review_candidate() docstrings for the real near-miss
        // this was built from) -- shown with the exact candidate so the
        // user can decide instead of this project silently guessing.
        if (r.last_search_result === 'needs_review' && r.review_candidate) {
          let candidate = null;
          try { candidate = JSON.parse(r.review_candidate); } catch (e) { candidate = null; }
          if (candidate) {
            const sizeGb = candidate.size ? `${(candidate.size / 1e9).toFixed(2)} GB` : 'unknown size';
            const reviewStatus = el('span', { className: 'srw-key-status' });
            const reviewBox = el('div', { className: 'srw-review-candidate' }, [
              el('span', {
                className: 'srw-review-candidate-title',
                textContent: 'Possible match: ',
                title: 'Named every performer credited for this scene, but the date/title '
                  + 'didn\'t line up closely enough to grab automatically.',
              }),
              // Links to the release's page on the indexer when known
              // (2026-09-26), to check it there before loading it.
              window.SRW.releaseTitleNode(candidate.title, candidate.infoUrl),
              el('span', {
                className: 'srw-review-candidate-meta',
                textContent: `${candidate.indexer || 'unknown indexer'} — ${sizeGb} — ${window.SRW.ageWords(candidate.age)}`,
              }),
              window.SRW.ageWarning(candidate.age, r.scene_date),
              el('button', {
                className: 'srw-btn srw-btn-primary',
                textContent: 'Load it',
                onClick: (e) => {
                  const btn = e.currentTarget;
                  window.SRW.confirmInline(btn,
                    `Download "${candidate.title}" for "${r.title}" even though it wasn't an automatic match?`,
                    async () => {
                      reviewStatus.textContent = ' starting...';
                      try {
                        const result = await window.SRW.runOp('releases_review_candidate_accept', {
                          provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
                        });
                        if (result.error) {
                          reviewStatus.textContent = ` error: ${result.error}`;
                          window.SRW.notify(`Loading failed: ${r.title || ''}`, result.error, 'error');
                          return;
                        }
                        window.SRW.notify(`Grabbed: ${r.title || ''}`, `${candidate.title} -- sent to the download client.`, 'success');
                        await renderCurrentView(container);
                      } catch (err) {
                        reviewStatus.textContent = ` error: ${err.message}`;
                      }
                    },
                  );
                },
              }),
              el('button', {
                className: 'srw-btn',
                textContent: 'Not this one',
                onClick: async (e) => {
                  e.target.disabled = true;
                  reviewStatus.textContent = ' ...';
                  try {
                    const result = await window.SRW.runOp('releases_review_candidate_reject', {
                      provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
                    });
                    if (result.error) { reviewStatus.textContent = ` error: ${result.error}`; e.target.disabled = false; return; }
                    window.SRW.dismissedNotice(`Rejected: ${window.SRW.cleanReleaseTitle ? window.SRW.cleanReleaseTitle(candidate.title) : candidate.title}`,
                      'Blocklisted for this scene.',
                      () => window.SRW.runOp('releases_review_candidate_restore', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }),
                      () => renderReleaseList(container));
                    await renderReleaseList(container);
                  } catch (err) {
                    reviewStatus.textContent = ` error: ${err.message}`;
                    e.target.disabled = false;
                  }
                },
              }),
              reviewStatus,
            ]);
            li.push(reviewBox);
          }
        }
      }
      // data-scene-id (2026-09-26): lets a Calendar pill jump to this row,
      // see jumpToWishlistRow().
      const rowClass = `srw-release-item${window.SRW.isLateAdditionNew(r) ? ' srw-late-new' : ''}`;
      ul.appendChild(arrangeReleaseRow(el('li', { className: rowClass, 'data-scene-id': r.scene_stashdb_id }, li), r));
    });

    // The section's live subscription (buildStatusSection()) updates
    // these bars; one immediate fetch so they don't start empty.
    if (status === 'sent_to_whisparr') {
      wishlistProgressElements = progressElements;
      if (progressElements.size > 0) updateDownloadProgress(progressElements);
    }
    if (status === 'imported') window.SRW.resolveSceneLinks(ul);
    return ul;
  }

  // Fetches the current SABnzbd progress for every 'sent_to_whisparr'
  // release with a download job (one batched call, see
  // routers/releases.py's download_progress()) and patches each
  // registered row's own progress bar/text in place. Stops its own
  // poll once nothing in `progressElements` is active any more (either
  // finished -- the periodic file-import loop will pick it up and
  // move the release out of this section on the next full render -- or
  // SABnzbd no longer reports it for some other reason); a manual or
  // periodic re-render restarts polling fresh via buildStatusSection()
  // above if there's something new to show.
  async function updateDownloadProgress(progressElements) {
    let data;
    try {
      data = await window.SRW.runOp('releases_download_progress');
    } catch (e) {
      return;
    }
    if (data.error) return;
    // Download AND import phases since 2026-09-26, see core.js's applyProgress().
    progressElements.forEach((elements, sceneId) => {
      window.SRW.applyProgress(elements, data[sceneId]);
    });
  }

  const STATUS_LABEL = {
    new: 'New', sent_to_whisparr: window.SRW.SENT_STATUS_LABEL, imported: 'Imported',
    import_failed: 'Import failed',
  };

  // "Stuck" (2026-09-21, user report -- a release could sit at
  // 'sent_to_whisparr' with a search result meaning "nothing usable
  // found", invisible on the Calendar since it still just looked like
  // any other on-wishlist pill) -- not a real `status` value, a
  // computed sub-state: searched at least once (`last_search_result`
  // set), but never actually grabbed (`download_job_id` still null).
  // Every RESULT_* indexer_search.py can record short of an actual grab
  // counts -- there's no meaningful further distinction worth a pill
  // color each (the exact reason is still available via the List
  // view's own searchStatusLabel()).
  // Opens the "On Wishlist" status section on this page and scrolls to /
  // briefly highlights the given scene's row (2026-09-26).
  async function jumpToWishlistRow(container, sceneId, title) {
    let row = container.querySelector(`.srw-releases-page-list li[data-scene-id="${CSS.escape(sceneId)}"]`);
    // Paged since 2026-09-26: the row may be on another page -- narrow the
    // wishlist section to this scene's title, then look again.
    // The sections are tabs now: show the wishlist tab first (its pane is hidden otherwise).
    const wishlistTab = [...container.querySelectorAll('.srw-section-tabs .srw-btn')][1];
    if (wishlistTab) wishlistTab.click();
    if (!row && title && statusSectionViews.sent_to_whisparr) {
      await statusSectionViews.sent_to_whisparr.setSearch(title);
      row = container.querySelector(`.srw-releases-page-list li[data-scene-id="${CSS.escape(sceneId)}"]`);
    }
    if (!row) return;
    const section = row.closest('details');
    if (section) section.open = true;
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.add('srw-row-highlight');
    setTimeout(() => row.classList.remove('srw-row-highlight'), 2500);
  }

  function isStuck(r) {
    return r.status === 'sent_to_whisparr' && !r.download_job_id && !!r.last_search_result;
  }

  // "Search visibility" label lookup moved to core.js (window.SRW.
  // searchStatusLabel) 2026-09-19, since the entity panel's "Missing
  // from Catalog" list needs the exact same lookup for the same
  // underlying release rows.

  // Calendar day cells cap how many pills show by default -- a single
  // very active day (dozens of releases, e.g. from a large ThePornDB
  // catch-up batch) would otherwise blow up that one cell's height and
  // break the grid's uniform layout. See buildCalendarGrid()'s "+N
  // more" toggle.
  const MAX_VISIBLE_PILLS_PER_DAY = 3;

  const STASHDB_ENDPOINT = 'https://stashdb.org/graphql';
  const THEPORNDB_ENDPOINT = 'https://theporndb.net/graphql';
  // Generic "download" glyph, embedded as a data URI rather than a
  // separate asset file -- keeps the plugin self-contained after a
  // plain scp deploy, no need to also verify how Stash serves
  // arbitrary static files out of a plugin's own directory. Replaces
  // the real Whisparr logo used here since 2026-09-12 -- retired
  // 2026-09-19 (Milestone 3), the user's own reasoning being that this
  // action no longer sends anything to Whisparr for search purposes
  // (only registers it there for bookkeeping, see handoff.py), so its
  // logo stopped being an accurate label; a plain download arrow reads
  // correctly regardless of which download client actually ends up
  // handling it. Same #6fa8e0 icon blue as the rest of the plugin's
  // own accent color, inline SVG (no external request, no recoloring
  // step needed unlike the old raster logo). This constant's name
  // (and the surrounding `srw-cal-pill-whisparr*` CSS classes, left
  // alone here) will likely need a broader pass once the project's own
  // name changes -- see the roadmap memory's "project rename" TODO,
  // raised the same day as this icon swap.
  function buildMagnifierIcon() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', '0 0 512 512');
    svg.setAttribute('class', 'srw-cal-pill-search-icon');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('fill', 'currentColor');
    path.setAttribute('d', 'M416 208c0 45.9-14.9 88.3-40 122.7L502.6 457.4c12.5 12.5 12.5 32.8 0 45.3s-32.8 12.5-45.3 0L330.7 376C296.3 401.1 253.9 416 208 416 93.1 416 0 322.9 0 208S93.1 0 208 0 416 93.1 416 208zM208 352a144 144 0 1 0 0-288 144 144 0 1 0 0 288z');
    svg.appendChild(path);
    return svg;
  }

  const DOWNLOAD_ICON_DATA_URI = 'data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20'
    + 'viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22%236fa8e0%22%20stroke-width%3D%222.5%22%20'
    + 'stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M21%2015v4a2%202%200%200%20'
    + '1-2%202H5a2%202%200%200%201-2-2v-4%22/%3E%3Cpolyline%20points%3D%227%2010%2012%2015%2017%2010%22/%3E%3Cline%20'
    + 'x1%3D%2212%22%20y1%3D%2215%22%20x2%3D%2212%22%20y2%3D%223%22/%3E%3C/svg%3E';
  // "Open in Stash" arrow, same line style as the download icon; both are drawn as a CSS mask (see .srw-cal-pill-whisparr-icon) so they take
  // the colour of the pill like the camera/person/x icons do, whatever the status colour (v0.129.2).
  const OPEN_ICON_DATA_URI = 'data:image/svg+xml,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20'
    + 'viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22%23000%22%20stroke-width%3D%222.5%22%20'
    + 'stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22M6%2018L18%206%22/%3E%3Cpolyline%20'
    + 'points%3D%229%206%2018%206%2018%2015%22/%3E%3C/svg%3E';
  // Studio logo / performer photo per (entity_type, entity_stashdb_id),
  // resolved from this Stash instance's own library (not StashDB
  // directly -- only entities that exist locally have a local image to
  // show) and cached module-wide so switching months or re-rendering
  // doesn't re-resolve images for studios/performers already seen this
  // session. `null` is cached too -- an entity with no local match or no
  // image is a permanent miss until the page reloads, not worth retrying.
  const entityImageCache = new Map();
  // Remembered for the browser session (v0.175.0): the pictures of the watched performers / studios were asked from Stash one by one on every
  // load of the calendar. A picture that is not found is not remembered, so it is tried again.
  const IMAGE_STORE_KEY = 'srw:entityImages';
  try {
    Object.entries(JSON.parse(window.sessionStorage.getItem(IMAGE_STORE_KEY) || '{}')).forEach(([k, v]) => entityImageCache.set(k, v));
  } catch (e) { /* no session storage: the cache just starts empty */ }
  function rememberEntityImages() {
    try {
      const obj = {};
      entityImageCache.forEach((v, k) => { if (v) obj[k] = v; });
      window.sessionStorage.setItem(IMAGE_STORE_KEY, JSON.stringify(obj));
    } catch (e) { /* ignore */ }
  }

  async function resolveEntityImage(entityType, entityStashId, provider) {
    const key = `${provider}:${entityType}:${entityStashId}`;
    if (entityImageCache.has(key)) return entityImageCache.get(key);

    const endpoint = provider === 'theporndb' ? THEPORNDB_ENDPOINT : STASHDB_ENDPOINT;
    const isStudio = entityType === 'studio';
    const query = isStudio
      ? `query($sid: String!) { findStudios(studio_filter: {stash_id_endpoint: {stash_id: $sid, endpoint: "${endpoint}", modifier: EQUALS}}, filter: {per_page: 1}) { studios { image_path } } }`
      : `query($sid: String!) { findPerformers(performer_filter: {stash_id_endpoint: {stash_id: $sid, endpoint: "${endpoint}", modifier: EQUALS}}, filter: {per_page: 1}) { performers { image_path } } }`;
    let imagePath = null;
    try {
      const response = await fetch('/graphql', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, variables: { sid: entityStashId } }),
      });
      const result = await response.json();
      const list = isStudio ? result?.data?.findStudios?.studios : result?.data?.findPerformers?.performers;
      imagePath = window.SRW.stashUrl(list?.[0]?.image_path) || null;
    } catch (e) {
      imagePath = null;
    }
    entityImageCache.set(key, imagePath);
    return imagePath;
  }

  // Text search matches title OR studio name (case-insensitive
  // substring) -- same two fields the pill itself displays, so "does
  // this match what I typed" is never a mystery. Type filter checks
  // `entity_types` (an array -- a merged release can have surfaced via
  // both a studio and a performer), same semantics as the Watchlist's
  // own filter.
  function filterCalendarReleases(releases) {
    const q = calendarSearchText.trim().toLowerCase();
    return releases.filter((r) => {
      if (!calendarProviderFilter[r.provider]) return false;
      if (r.status === 'imported' && !calendarShowImported) return false;
      if (calendarTypeFilter !== 'both' && !r.entity_types.includes(calendarTypeFilter)) return false;
      if (calendarVrFilter === 'vr' && !r.is_vr) return false;
      if (calendarVrFilter === 'non_vr' && r.is_vr) return false;
      if (q) {
        const haystack = `${r.title || ''} ${r.studio_name || ''} ${calendarSearchDescription ? r.details || '' : ''} ${calendarSearchTags ? (r.tags || '').split('|').join(' ') : ''}`
          .toLowerCase();
        if (!haystack.includes(q)) return false;
      }
      return true;
    });
  }

  // The scene calendar's own filter state, as one object so the very same filter bar can serve other lists (the movie page passes its own
  // state object to buildCalendarFilterBar(); v0.138.0). The accessors keep reading/writing the module variables the scene code uses.
  const sceneFilterState = {
    get search() { return calendarSearchText; }, set search(v) { calendarSearchText = v; },
    get description() { return calendarSearchDescription; }, set description(v) { calendarSearchDescription = v; },
    get tags() { return calendarSearchTags; }, set tags(v) { calendarSearchTags = v; },
    get type() { return calendarTypeFilter; }, set type(v) { calendarTypeFilter = v; },
    get vr() { return calendarVrFilter; }, set vr(v) { calendarVrFilter = v; },
    get providers() { return calendarProviderFilter; },
    get showImported() { return calendarShowImported; }, set showImported(v) { calendarShowImported = v; },
  };

  function buildCalendarFilterBar(onFilterChange, state = null, opts = {}) {
    const S = state || sceneFilterState;
    const noun = opts.noun || 'scene';
    const importedKey = opts.importedStoreKey || 'srw:calendar-show-imported';
    const { el } = window.SRW;
    const searchInput = el('input', {
      type: 'search', placeholder: 'Search title or studio…', value: S.search,
      className: 'srw-search-input',
      oninput: (e) => { S.search = e.target.value; onFilterChange(); },
    });
    const descCheckbox = el('input', { type: 'checkbox' });
    descCheckbox.checked = S.description;
    descCheckbox.addEventListener('change', () => { S.description = descCheckbox.checked; onFilterChange(); });
    const descToggle = el('label', {
      className: 'srw-provider-filter-item', title: `Also match the ${noun} description, not only title/studio`,
    }, [descCheckbox, ' incl. description']);
    const tagsCheckbox = el('input', { type: 'checkbox' });
    tagsCheckbox.checked = S.tags;
    tagsCheckbox.addEventListener('change', () => { S.tags = tagsCheckbox.checked; onFilterChange(); });
    const tagsToggle = el('label', {
      className: 'srw-provider-filter-item', title: `Also match the ${noun}'s tags, not only title/studio`,
    }, [tagsCheckbox, ' incl. tags']);
    // Drop-downs like the other lists' filter bars (v0.225.5); the function names stay for the presets that set the values.
    const typeSelect = window.SRW.buildSelect(
      [{ key: 'both', label: 'All types' }, { key: 'studio', label: 'Studios' }, { key: 'performer', label: 'Performer' }],
      () => S.type, (v) => { S.type = v; onFilterChange(); }, opts.typeTitle || 'Came in through a watched studio or a watched performer');
    function updateActiveFilterBtn() { typeSelect.value = S.type; }
    const vrSelect = window.SRW.buildSelect(
      [{ key: 'all', label: 'VR + Non-VR' }, { key: 'vr', label: 'VR' }, { key: 'non_vr', label: 'Non-VR' }],
      () => S.vr, (v) => { S.vr = v; onFilterChange(); }, 'VR or flat releases');
    function updateActiveVrBtn() { vrSelect.value = S.vr; }
    // `opts.providerKeys`: the providers this list can contain (the Movies lists only have ThePornDB); default: every provider.
    const providerBoxes = buildProviderCheckboxes(S.providers, onFilterChange, opts.providerKeys || null);
    const importedCheckbox = el('input', { type: 'checkbox' });
    importedCheckbox.checked = S.showImported;
    importedCheckbox.addEventListener('change', () => {
      S.showImported = importedCheckbox.checked;
      try { localStorage.setItem(importedKey, S.showImported ? '1' : '0'); } catch (e) { /* ignore */ }
      onFilterChange();
    });
    const importedToggle = el('label', {
      className: 'srw-provider-filter-item', title: `Also show the green pills of ${noun}s that were already imported`,
    }, [importedCheckbox, ' Show imported']);
    // Calendar has no date-range filter (the month is its range) --
    // a preset saved here stores empty dates, and one saved from the
    // List view just has its dates ignored here.
    const presetControl = buildPresetControl(
      () => ({
        search: S.search, description: S.description, tags: S.tags, type: S.type, vr: S.vr,
        providers: { ...S.providers }, date_from: '', date_until: '',
      }),
      (p) => {
        S.search = p.search || '';
        S.description = Boolean(p.description);
        descCheckbox.checked = S.description;
        S.tags = Boolean(p.tags);
        tagsCheckbox.checked = S.tags;
        S.type = p.type || 'both';
        S.vr = p.vr || 'all';
        Object.assign(S.providers, { stashdb: true, theporndb: true }, p.providers || {});
        searchInput.value = S.search;
        updateActiveFilterBtn();
        updateActiveVrBtn();
        syncProviderCheckboxes(providerBoxes, S.providers);
        onFilterChange();
      },
    );
    return el('div', { className: 'srw-filter-bar' }, [
      opts.noPresets ? null : presetControl,
      searchInput,
      descToggle,
      tagsToggle,
      typeSelect,
      vrSelect,
      providerBoxes,
      opts.noImported ? null : importedToggle,
    ]);
  }

  async function renderCalendarView(container) {
    await renderCalendarViewInner(container);
    tabifyReleaseSections(container);
  }

  async function renderCalendarViewInner(container) {
    const { el } = window.SRW;
    const listWrap = container.querySelector('.srw-releases-page-list');
    listWrap.textContent = 'Loading...';

    // Whisparr's own Calendar plots every tracked movie by release date
    // color-coded by status, not just "new" ones -- same idea here: pull
    // new/sent/imported/failed together (skip dismissed, there's nothing
    // to see) so a whole month's picture is visible in one place, same
    // reasoning as PROGRESS.md's original "modeled loosely on Whisparr's
    // Calendar view" note. `failed` added 2026-09-21 alongside the new
    // .srw-cal-pill-import_failed color -- previously never fetched for
    // the Calendar at all, so an import failure was invisible here
    // regardless of pill color.
    //
    // 2026-09-26: one query for exactly the displayed month (all four
    // statuses) instead of four "newest 1000" lists -- with ~1700+ 'new'
    // releases those lists silently dropped older months' entries.
    // calendarMonth is resolved here already (was further down) since
    // the query needs its date range.
    if (!calendarMonth) {
      // Default to the real current month (matches Whisparr's own
      // calendar default), not "most recent release" -- releases here
      // can span years (scene_date is the shoot date, StashDB backfills
      // old catalogs), so anchoring on today is the more useful default.
      const now = new Date();
      calendarMonth = new Date(now.getFullYear(), now.getMonth(), 1);
    }
    const monthStart = `${calendarMonth.getFullYear()}-${String(calendarMonth.getMonth() + 1).padStart(2, '0')}-01`;
    const monthEndDate = new Date(calendarMonth.getFullYear(), calendarMonth.getMonth() + 1, 0);
    const monthEnd = `${monthStart.slice(0, 8)}${String(monthEndDate.getDate()).padStart(2, '0')}`;
    const sectionsPromise = buildStatusSections(container); // runs while the month and the pictures load
    const [monthResult, settings] = await Promise.all([
      window.SRW.runOp('releases_query', {
        statuses: ['new', 'sent_to_whisparr', 'imported', 'import_failed'],
        date_from: monthStart, date_until: monthEnd, per_page: 0,
      }),
      window.SRW.runOp('settings_get_all'),
    ]);
    loadFilterPresets(settings);
    if (monthResult.error) {
      listWrap.textContent = `Error: ${monthResult.error}`;
      return;
    }
    const fresh = monthResult.rows.filter((r) => r.status === 'new');
    const all = groupByScene(monthResult.rows);
    // Declared here (not down near where it's first used below) so
    // rebuildGrid() -- which runs immediately once, before that later
    // code -- can already safely reference it via closure.
    const idToRelease = new Map(fresh.map((r) => [r.scene_stashdb_id, r]));

    // Resolve every distinct entity's image once, in parallel, up front
    // for the *unfiltered* set -- so toggling the filter bar afterwards
    // never needs a new image fetch, just a re-filter of already-
    // resolved data.
    const distinctEntities = new Map();
    all.forEach((r) => distinctEntities.set(`${r.provider}:${r.entity_type}:${r.entity_stashdb_id}`, r));
    await Promise.all(Array.from(distinctEntities.values()).map((r) =>
      resolveEntityImage(r.entity_type, r.entity_stashdb_id, r.provider)
    ));
    rememberEntityImages();
    const statusSections = await sectionsPromise;

    // The filter bar is built once per full render and left alone after
    // that -- only `gridWrap`'s contents get replaced on a filter
    // change, so the search <input> (and its cursor position/focus)
    // survives every keystroke instead of being torn down and rebuilt.
    listWrap.innerHTML = '';

    // Same three sections as the List view (2026-09-20, user request --
    // previously Calendar-only view had no way to see what happened to
    // a hand-off once it scrolled off the grid). Reuses buildStatusSection()
    // as-is, including its own download-progress polling/Restore button
    // -- a second, smaller-limit fetch of the same sent/imported data
    // this view already pulled above for the grid itself, but keeping
    // it a single shared function beat duplicating that logic here.
    statusSections.forEach((node) => listWrap.appendChild(node));

    const gridWrap = el('div');
    // Populated once the batched Whisparr check below comes back --
    // rebuildGrid() re-applies it (synchronously, no re-fetch) on every
    // call, including ones triggered later by the filter bar, so a
    // search/filter change doesn't revert already-patched pills back
    // to their "Add to Wishlist" default (2026-09-13).
    let whisparrStatusCache = null;
    const bulkSlot = el('div');
    function rebuildGrid() {
      const filtered = filterCalendarReleases(all);
      // Bulk dismiss scoped to the displayed month only -- that's all
      // the user can actually see and check before confirming.
      const monthPrefix = `${calendarMonth.getFullYear()}-${String(calendarMonth.getMonth() + 1).padStart(2, '0')}`;
      const bulkTargets = filtered.filter((r) => r.status === 'new'
        && (r.scene_date || r.scene_created.slice(0, 10)).startsWith(monthPrefix));
      const filterActive = Boolean(calendarSearchText.trim() || calendarTypeFilter !== 'both' || calendarVrFilter !== 'all'
        || !window.SRW.providersAllOn(calendarProviderFilter));
      bulkSlot.innerHTML = '';
      bulkSlot.appendChild(buildBulkDismissSlot(bulkTargets, filterActive, container));
      const byDay = {};
      filtered.forEach((r) => {
        const day = r.scene_date || r.scene_created.slice(0, 10);
        (byDay[day] = byDay[day] || []).push(r);
      });
      // Stable per-day order, independent of which of the four status
      // fetches above contributed a given release -- 2026-09-22, user
      // report: hitting "Add to Wishlist" on a Calendar pill made it
      // seem to vanish. It hadn't (status correctly became
      // sent_to_whisparr) -- it had silently moved to the END of its
      // day's list, since `all` concatenates fresh/sent/imported/failed
      // in that fixed order and this forEach just pushes in that same
      // order, with no sort of its own. A release that had been near
      // the top of its day (visible) could jump straight past
      // MAX_VISIBLE_PILLS_PER_DAY the instant its status changed, with
      // nothing else about that day actually different. `scene_created`
      // descending matches release_repo.list_releases()'s own existing
      // ORDER BY for the 'new' list -- confirmed live this reproduces
      // the exact pre-bug visible order (the fetch order a same-day
      // 'new'-only view already had), so this sort doesn't just make
      // ordering *stable*, it keeps a release's position exactly where
      // it already was, before and after any status change.
      Object.values(byDay).forEach((day) => day.sort((a, b) => b.scene_created.localeCompare(a.scene_created)));
      gridWrap.innerHTML = '';
      gridWrap.appendChild(buildCalendarGrid(calendarMonth, byDay, container));
      gridWrap.appendChild(buildCalendarLegend());
      if (whisparrStatusCache) applyCalendarWhisparrStatus(gridWrap, whisparrStatusCache, idToRelease, container);
    }
    listWrap.appendChild(buildCalendarFilterBar(rebuildGrid));
    listWrap.appendChild(bulkSlot);
    listWrap.appendChild(gridWrap);
    rebuildGrid();

    // Fire-and-forget, mirrors the List view's annotateWhisparrStatus()
    // -- 'new'-status only, sent/imported pills already reflect their
    // outcome via status color and don't need this. Scoped to pills
    // actually rendered in the DOM right now, not all of `fresh`
    // (2026-09-13, caught live: `fresh` is every 'new' release
    // site-wide, up to 1000 -- releases here can span years of
    // backfilled StashDB history, see this function's own "anchoring
    // on today" comment above, and even one month can hold 200+, most
    // of them hidden behind "+N more" per day -- see
    // MAX_VISIBLE_PILLS_PER_DAY. Querying the DOM's own data-scene-id
    // attributes (set by buildPill()) rather than re-deriving "visible"
    // from date math automatically matches whatever's actually on
    // screen, including the per-day cap, without duplicating that
    // logic here.
    const visibleIds = Array.from(gridWrap.querySelectorAll('[data-scene-id]'))
      .map((elm) => elm.dataset.sceneId);
    const scenes = visibleIds
      .map((id) => idToRelease.get(id))
      .filter((r) => r && r.provider === 'stashdb')
      .map((r) => ({ scene_stashdb_id: r.scene_stashdb_id, is_vr: !!r.is_vr }));
    if (scenes.length) {
      window.SRW.runOp('releases_library_status', { scenes }).then((status) => {
        if (status.error) return;
        whisparrStatusCache = status;
        applyCalendarWhisparrStatus(gridWrap, status, idToRelease, container);
      }).catch(() => {});
    }
  }

  function applyCalendarWhisparrStatus(gridWrap, status, idToRelease, container) {
    Object.entries(status).forEach(([sceneId, info]) => {
      const pill = gridWrap.querySelector(`[data-scene-id="${CSS.escape(sceneId)}"]`);
      if (!pill) return;
      const btn = pill.querySelector('.srw-cal-pill-whisparr');
      if (!btn) return;
      if (info.whisparr_file_mismatch) {
        // Mirrors the List/Panel views' handling -- no room for a
        // separate warning badge in a pill this size, so the title
        // (tooltip) carries the explanation instead. Rebinds like the
        // List view does: cloneNode strips the old confirm-and-
        // handoff listener before wiring the fix-mismatch one on.
        const r = idToRelease.get(sceneId);
        if (r) {
          const fixBtn = btn.cloneNode(true);
          fixBtn.title = `Wrong file: found in your library as "${info.mismatch_title}" -- click to fix`;
          fixBtn.addEventListener('click', () => {
            const title = r.title || r.scene_stashdb_id;
            window.SRW.confirmInline(fixBtn,
              `Whisparr's file for "${title}" doesn't match this scene -- delete the wrong file from `
              + `Whisparr (and disk) and have it search for a correct release instead?`,
              async () => {
                const result = await window.SRW.runOp('releases_fix_mismatch', {
                  provider: r.provider, entity_type: r.entity_type,
                  entity_stashdb_id: r.entity_stashdb_id, scene_stashdb_id: r.scene_stashdb_id,
                });
                if (result.error) { window.SRW.notify('Fix wrong download', `Fix failed: ${result.error}`, 'error'); return; }
                await renderCalendarView(container);
              },
              { danger: true },
            );
          });
          btn.replaceWith(fixBtn);
        }
      } else if (info.whisparr_has_file) {
        btn.remove();
      } else if (info.whisparr_exists) {
        btn.title = 'Search now';
      }
    });
  }

  function buildCalendarGrid(month, byDay, container) {
    const { el } = window.SRW;
    const year = month.getFullYear();
    const monthIndex = month.getMonth();
    const monthName = month.toLocaleDateString(undefined, { month: 'long', year: 'numeric' });

    const header = el('div', { className: 'srw-cal-header' }, [
      el('button', {
        className: 'srw-btn srw-btn-sm', textContent: '‹ Prev',
        onClick: async () => {
          calendarMonth = new Date(year, monthIndex - 1, 1);
          await renderCalendarView(container);
        },
      }),
      el('span', { className: 'srw-cal-title', textContent: monthName }),
      el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Next ›',
        onClick: async () => {
          calendarMonth = new Date(year, monthIndex + 1, 1);
          await renderCalendarView(container);
        },
      }),
      el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Today',
        onClick: async () => {
          calendarMonth = null;
          await renderCalendarView(container);
        },
      }),
    ]);

    const grid = el('div', { className: 'srw-cal-grid' });
    ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach((d) => {
      grid.appendChild(el('div', { className: 'srw-cal-weekday', textContent: d }));
    });

    const firstOfMonth = new Date(year, monthIndex, 1);
    const daysInMonth = new Date(year, monthIndex + 1, 0).getDate();
    // Monday-first week -- getDay() is 0=Sun..6=Sat, shift so Monday=0.
    const leadingBlanks = (firstOfMonth.getDay() + 6) % 7;

    const todayKey = new Date().toISOString().slice(0, 10);
    for (let i = 0; i < leadingBlanks; i++) {
      grid.appendChild(el('div', { className: 'srw-cal-day srw-cal-day-blank' }));
    }
    for (let d = 1; d <= daysInMonth; d++) {
      const dateKey = `${year}-${String(monthIndex + 1).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
      const dayReleases = byDay[dateKey] || [];
      const dayCell = el('div', {
        className: 'srw-cal-day' + (dateKey === todayKey ? ' srw-cal-day-today' : ''),
      }, [
        el('div', { className: 'srw-cal-daynum', textContent: String(d) }),
      ]);
      const buildPill = (r) => {
        const key = `${r.provider}:${r.entity_type}:${r.entity_stashdb_id}`;
        const imagePath = entityImageCache.get(key);
        let avatar;
        if (imagePath) {
          avatar = el('img', {
            className: `srw-cal-avatar srw-cal-avatar-${r.entity_type}`,
            src: imagePath, alt: '', loading: 'lazy',
          });
        } else {
          avatar = window.SRW.entityIcon(r.entity_type);
          if (avatar) avatar.classList.add('srw-cal-avatar-fallback');
        }
        const sourceNote = r.provider !== 'stashdb' ? ` [${window.SRW.providerLabel(r.provider)}]` : '';
        const entityNote = r.entity_types.length > 1 ? ` (via ${r.entity_types.join(' & ')})` : '';
        // A <div> wrapper, not a single <a> like before this pill also
        // needed its own separate local-entity link (2026-09-12) --
        // nesting an <a> inside an <a> is invalid HTML and behaves
        // inconsistently across browsers, so the scene link and the
        // local-page link are now two siblings instead of one link
        // wrapping everything. See buildLocalEntityLink()'s docstring
        // for why this is a second, additional link rather than folded
        // into the existing entity icon.
        const sceneLink = el('a', {
          className: 'srw-cal-pill-link',
          href: sceneUrl(r.provider, r.scene_stashdb_id),
          target: '_blank', rel: 'noopener noreferrer',
          title: `${r.title || '(untitled)'} — ${r.studio_name} (${STATUS_LABEL[r.status] || r.status})${sourceNote}${entityNote}`,
        }, [
          avatar,
          el('span', { className: 'srw-cal-pill-text' }, [
            el('span', { className: 'srw-cal-pill-title', textContent: r.title || '(untitled)' }),
            el('span', { className: 'srw-cal-pill-studio', textContent: r.studio_name }),
          ]),
        ]);
        // iconOnly: this pill already shows the studio name itself via
        // .srw-cal-pill-studio above -- see buildLocalEntityLink()'s own
        // comment for the duplicate-name regression this avoids.
        const localLink = buildLocalEntityLink(r, { iconOnly: true });
        // A ThePornDB-sourced pill gets its own color instead of the
        // status color -- safe to fully override rather than just
        // accenting it, since a ThePornDB release can structurally
        // never reach 'sent_to_whisparr'/'imported' (handoff.py rejects
        // any provider != 'stashdb' before setting either status), so
        // this class only ever coexists with 'new' in practice. Same
        // amber used for the "ThePornDB" badge on the Watchlist page,
        // per the user's own reference point.
        // Only while still 'new' (2026-09-25): ThePornDB releases can now
        // reach every other status too, and those colors must win.
        const providerClass = r.provider !== 'stashdb' && r.status === 'new' ? ` srw-cal-pill-${r.provider}` : '';
        // Overrides the plain 'sent_to_whisparr' color -- see isStuck()'s
        // own comment. Declared after it in the CSS so it wins (same
        // last-one-wins convention providerClass above already uses).
        const stuckClass = isStuck(r) ? ' srw-cal-pill-stuck' : '';
        // Same action as the List view's "Search now" button, as
        // an icon here instead of a text label -- no room for that in a
        // pill this size. StashDB-only, same reasoning as the List
        // view's isTpdb branch (Whisparr can't look up a ThePornDB
        // scene id). The real Whisparr logo, not a generic glyph
        // (2026-09-12, user found the original "↑" both unrecognizable
        // and too small/fiddly a click target) -- the <button> itself
        // carries the click-area padding, sized well past the icon's
        // own visual bounds for an easier hit target.
        // Defaults to "Add to Wishlist" -- annotateCalendarWhisparrStatus()
        // below (2026-09-13) swaps the title to "Search now" if
        // this scene turns out to already be tracked in Whisparr
        // without a file, or removes the button entirely if it already
        // has one (same three-way check as the List view/Studio-
        // Performer panel, just without room for a text label or a
        // separate badge in a pill this size).
        const whisparrBtn = r.status === 'new' ? el('button', {
          className: 'srw-cal-pill-whisparr',
          title: 'Add to Wishlist',
          onClick: async (e) => {
            const doHandoff = async () => {
              const result = await window.SRW.runOp('releases_handoff', {
                provider: r.provider, entity_type: r.entity_type,
                entity_stashdb_id: r.entity_stashdb_id, scene_stashdb_id: r.scene_stashdb_id,
              });
              if (result.error) { window.SRW.notify(`Adding failed: ${r.title || ''}`, result.error, 'error'); return; }
              window.SRW.notifyHandoff(r.title, result);
              await renderCalendarView(container);
            };
            // "Search now" fires immediately, no confirm() -- see
            // entity-panel.js's confirmHandoff() note (2026-09-19).
            // Short "Add to Wishlist?" text, unlike every other
            // confirmInline() call site for this same action (List
            // view/entity panel/Search page) -- 2026-09-22, user report:
            // confirmInline() inserts its prompt inline right after the
            // trigger button (core.js), and a Calendar pill is a small
            // fixed-width cell that already shows the scene title and
            // studio name right next to this button, so the usual
            // `Add "${title}" to your wishlist and search for it?`
            // both overflowed the cell and repeated text already
            // visible. "and search for it" dropped too -- there's no
            // separate "just add, don't search" option offered, so the
            // question was never actually optional information.
            if (e.currentTarget.title !== 'Search now') {
              window.SRW.confirmInline(e.currentTarget, 'Add to Wishlist?', doHandoff);
              return;
            }
            await doHandoff();
          },
        }, [el('span', { className: 'srw-cal-pill-whisparr-icon', style: `--srw-icon: url("${DOWNLOAD_ICON_DATA_URI}")` })]) : null;
        // Same action as the List view's "Dismiss" button, missing here
        // until now (2026-09-12).
        const dismissBtn = el('button', {
          className: 'srw-cal-pill-dismiss',
          title: 'Dismiss',
          textContent: '×',
          onClick: async () => {
            const res = await window.SRW.dismissRelease(r);
            window.SRW.releaseDismissedNotice(r, res, () => renderCalendarView(container));
            await renderCalendarView(container);
          },
        });
        // A bottom action row, not inline with the title/studio text
        // (changed 2026-09-12) -- three icons squeezed vertically-
        // centered alongside the text was eating too much of the pill's
        // width, and the studio line usually has slack under it anyway
        // (it's shorter than the title in most cases) that this row can
        // sit in instead.
        // "Searched, nothing usable found" (2026-09-26, user request): no
        // room in a pill for the Whisparr fallback / manual search, so
        // jump to this release's row in the "On Wishlist" section above
        // the calendar, which has them.
        // Magnifier SVG sized like the other pill icons (2026-09-26: the
        // 🔍 emoji inherited the "×" button's 32px font size and looked
        // far too big).
        const searchJumpBtn = isStuck(r) ? el('button', {
          className: 'srw-cal-pill-dismiss srw-cal-pill-search', title: 'Search options (Last search, fallback, own query)',
          onClick: (e) => { e.stopPropagation(); jumpToWishlistRow(container, r.scene_stashdb_id, r.title); },
        }, [buildMagnifierIcon()]) : null;
        // Downloaded (imported) scenes: the scene in Stash, same place as the other icons; the link is set per month (resolveSceneLinks).
        let openBtn = null;
        if (r.status === 'imported') {
          openBtn = el('a', { className: 'srw-cal-pill-dismiss srw-cal-pill-open srw-open-scene', target: '_blank',
            rel: 'noopener noreferrer', hidden: true, title: 'Open this scene in Stash (new tab)', 'aria-label': 'Open scene' },
            [el('span', { className: 'srw-cal-pill-whisparr-icon', style: `--srw-icon: url("${OPEN_ICON_DATA_URI}")` })]);
          openBtn.dataset.provider = r.provider; openBtn.dataset.scene = r.scene_stashdb_id;
          openBtn.addEventListener('click', (e) => e.stopPropagation());
        }
        // "Hide studio" from the Calendar (v0.215.2), same action and condition as the List view's button: only releases that came in through a
        // watched STUDIO (the sub-studio of a watched network); the studio's other releases leave the Calendar and List, Undo brings them back.
        let hideBtn = null;
        if (r.status === 'new' && ((r.entity_types || []).includes('studio') || r.entity_type === 'studio') && !/^(upgrade|film):/.test(String(r.entity_stashdb_id || ''))) {
          hideBtn = el('button', {
            className: 'srw-cal-pill-dismiss', title: `Hide the studio “${r.studio_name || 'this studio'}” (its releases that came in through a watched studio)`,
            'aria-label': 'Hide studio',
          }, [window.SRW.icon('faEyeSlash', 'srw-cal-pill-hide-icon') || document.createTextNode('⊘')]);
          hideBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            window.SRW.confirmInline(hideBtn,
              `Hide the studio “${r.studio_name || '?'}”? Its releases that came in through a watched studio leave the Calendar and List (nothing is deleted; Undo or Settings > Hidden studios shows them again).`,
              async () => {
                const result = await window.SRW.runOp('muted_studios_mute', { provider: r.provider, scene_id: r.scene_stashdb_id });
                if (result.error) { window.SRW.notify('Hide studio', result.error, 'error'); return; }
                window.SRW.dismissedNotice('Studio hidden', `“${result.name || r.studio_name}” hidden (${result.hidden} release(s)).`,
                  () => window.SRW.runOp('muted_studios_unmute', { studio_id: result.studio_id }), () => renderCalendarView(container));
                await renderCalendarView(container);
              });
          });
        }
        const actions = el('div', { className: 'srw-cal-pill-actions' },
          [localLink, whisparrBtn, searchJumpBtn, openBtn, hideBtn, dismissBtn].filter(Boolean));
        const pill = el('div', {
          className: `srw-cal-pill srw-cal-pill-${r.status}${stuckClass}${providerClass}`,
        }, [sceneLink, actions]);
        pill.dataset.sceneId = r.scene_stashdb_id;
        return pill;
      };

      // Cap pills shown per day -- a single very active day (e.g. one
      // with dozens of releases) would otherwise blow up that one
      // cell's height and break the grid's uniform layout. Only the
      // clicked day expands, in place, to show the rest -- other days
      // are untouched, same "surface what fits, reveal more on demand"
      // idea as the List view's collapsed date groups.
      const visible = dayReleases.slice(0, MAX_VISIBLE_PILLS_PER_DAY);
      const hidden = dayReleases.slice(MAX_VISIBLE_PILLS_PER_DAY);
      visible.forEach((r) => dayCell.appendChild(buildPill(r)));
      if (hidden.length > 0) {
        // Toggle, not a one-way reveal (changed 2026-09-12 -- user found
        // expanding a day was a dead end, no way back to the compact
        // view without reloading). Keeps its own expanded pills around
        // so collapsing just removes exactly those again, rather than
        // re-deriving them from `hidden` (which stays the same
        // reference across toggles either way, but this reads clearer).
        let expanded = false;
        let expandedPills = [];
        const toggleBtn = el('button', {
          className: 'srw-cal-more-btn',
          textContent: `+${hidden.length} more`,
          onClick: () => {
            if (expanded) {
              expandedPills.forEach((pill) => pill.remove());
              expandedPills = [];
              toggleBtn.textContent = `+${hidden.length} more`;
            } else {
              expandedPills = hidden.map((r) => {
                const pill = buildPill(r);
                dayCell.insertBefore(pill, toggleBtn);
                return pill;
              });
              toggleBtn.textContent = 'Show less';
              window.SRW.resolveSceneLinks(dayCell); // the pills behind "+N more" are built only now: give their "↗" its link
            }
            expanded = !expanded;
          },
        });
        dayCell.appendChild(toggleBtn);
      }
      grid.appendChild(dayCell);
    }
    const trailingBlanks = (7 - ((leadingBlanks + daysInMonth) % 7)) % 7;
    for (let i = 0; i < trailingBlanks; i++) {
      grid.appendChild(el('div', { className: 'srw-cal-day srw-cal-day-blank' }));
    }

    const wrap = el('div', {}, [header, grid]);
    window.SRW.resolveSceneLinks(wrap); // one request for the month's downloaded pills
    return wrap;
  }

  function buildCalendarLegend() {
    const { el } = window.SRW;
    const statusItems = Object.entries(STATUS_LABEL).map(([status, label]) =>
      el('span', { className: 'srw-cal-legend-item' }, [
        el('span', { className: `srw-cal-legend-swatch srw-cal-pill-${status}` }),
        label,
      ])
    );
    // Separate from the status legend above -- this is a source, not a
    // status (a ThePornDB pill is always 'new', see buildPill()'s
    // providerClass comment), added alongside the pill's own color
    // change (2026-09-12).
    const providerItems = window.SRW.providerKeys().filter((k) => k !== 'stashdb').map((key) => el('span', { className: 'srw-cal-legend-item' }, [
      el('span', { className: `srw-cal-legend-swatch srw-cal-pill-${key}` }),
      `${window.SRW.providerLabel(key)} source`,
    ]));
    // "Stuck" (2026-09-21) is a computed sub-state of 'sent_to_whisparr'
    // (see isStuck()), not a real status value STATUS_LABEL iterates
    // over above -- needs its own manual legend entry, same reasoning
    // as providerItems above.
    const stuckItem = el('span', { className: 'srw-cal-legend-item' }, [
      el('span', { className: 'srw-cal-legend-swatch srw-cal-pill-stuck' }),
      'Searched, nothing usable found',
    ]);
    return el('div', { className: 'srw-cal-legend' }, [...statusItems, stuckItem, ...providerItems]);
  }

  window.SRW_Releases = {
    render,
    // For the movie page (v0.138.0): the same filter bar, presets, icons and legend as the scene calendar.
    buildFilterBar: (state, onChange, opts) => buildCalendarFilterBar(onChange, state, opts || {}),
    loadFilterPresets,
    icons: { DOWNLOAD: DOWNLOAD_ICON_DATA_URI, OPEN: OPEN_ICON_DATA_URI },
  };
})();
