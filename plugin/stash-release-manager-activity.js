/**
 * StashReleaseManager - Activity
 *
 * Consolidates what the Releases page's "On Wishlist"/"Imported"
 * sections already show (2026-09-21, user request after noticing
 * 'import_failed' releases had NO visible home anywhere in this
 * plugin's UI -- only a transient alert() after manually clicking "Run
 * file import now" on the Settings page) into one dedicated view:
 * what's currently downloading (live progress), what got imported, and
 * what failed to import. Read-only/informational by design -- the
 * triage actions (Search now, Fix wrong download, Restore a dismiss,
 * the review-candidate box) stay on the Releases page, which already
 * has them; this page is a complementary status view, not a
 * replacement.
 *
 * What to actually DO about an 'import_failed' release (retry? surface
 * the failure reason, which isn't persisted anywhere today? a manual
 * fix flow?) is explicitly a follow-up decision, not answered here --
 * this page only makes the existing gap visible.
 */
(function () {
  'use strict';

  // Live push since v0.78.0 (2026-09-26, see core.js's attachLive()):
  // detach() of the current subscription.
  let liveDetach = null;
  // Scene ids of every release in "Downloading & importing" at the last render.
  let listedDownloadIds = new Set();
  // Renders can overlap (a push event arrives while one is still loading):
  // only the newest one gets to put its result on the page.
  let renderSeq = 0;

  // Asks for the download status once (set by the page that is showing); called when the browser tab becomes visible again.
  let refreshProgress = null;
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && refreshProgress && document.querySelector('.srw-activity-page')) refreshProgress();
  });

  let uploadEnabled = false; // Settings > fingerprint_upload_enabled, set by render(), read by buildSection()'s Import failed rows

  async function render(container) {
    const { el } = window.SRW;
    const mySeq = ++renderSeq;
    // The old subscription is dropped only once the new one is attached (below): with no listener left in between, the live connection
    // closes and has to be opened again, and the status only arrives after that.
    const previousDetach = liveDetach;
    liveDetach = null;
    // The status of the downloads is asked for right away, in parallel with building the page, and put on the rows as soon as they exist.
    const progressRequest = window.SRW.runOp('releases_download_progress').catch(() => null);
    // The same for what the Stuck tab adds.
    const stuckExtras = Promise.all([
      window.SRW.runOp('indexer_search_stuck_preview').catch(() => null),
      window.SRW.runOp('films_stuck_preview').catch(() => null),
      window.SRW.runOp('torrents_health').catch(() => null),
    ]);
    // Built off-page and swapped in at the end -- a re-render keeps the
    // current content visible until the new one is ready (no flicker).
    const root = el('div');
    root.appendChild(window.SRW.buildPageHeader('activity'));
    const hint = window.SRW.buildLiveHint();
    hint.classList.add('srw-activity-page'); // marks "the Activity page is showing"
    root.appendChild(hint);
    const wrap = el('div');
    root.appendChild(wrap);
    if (!container.querySelector('.srw-activity-page')) {
      container.replaceChildren();
      container.appendChild(window.SRW.buildPageHeader('activity'));
      container.appendChild(el('div', { className: 'srw-activity-page', textContent: 'Loading...' }));
    }
    // Only if this is still the newest render AND the user hasn't moved
    // on to another page meanwhile (they share `container`).
    const show = () => {
      if (mySeq === renderSeq && container.querySelector('.srw-activity-page')) {
        container.replaceChildren(...root.childNodes);
      }
    };
    // Must resolve before any row builds a scene link (sceneUrl() reads
    // this cache synchronously) -- see core.js's own comment.
    await window.SRW.loadMetadataProviderUrls();

    // All rows, no hidden cap (2026-09-26: was `limit: 500` per list) --
    // per_page 0 = unpaged; each section below pages + filters itself.
    uploadEnabled = false; // read from the settings just below, used by the Import failed rows (module-level: buildSection() reads it)
    // Settings > Content = movies only: no scene queries at all (same as the Releases page).
    const scenesOn = await window.SRW.scopeAllows('scenes');
    const fetchAll = (status) => (scenesOn ? window.SRW.runOp('releases_query', { statuses: [status], per_page: 0 }) : { rows: [] });
    // "Imported" moved to the History page (2026-09-28): Activity shows
    // only what's running or needs attention, like the *arrs.
    const [responses, reviews, appSettings, filmsData] = await Promise.all([
      Promise.all([fetchAll('sent_to_whisparr'), fetchAll('import_failed')]),
      scenesOn ? window.SRW.runOp('releases_reviews') : {},
      window.SRW.runOp('settings_get_all').catch(() => ({})),
      (await window.SRW.scopeAllows('movies')) ? window.SRW.runOp('films_activity').catch(() => ({ rows: [] })) : { rows: [] }, // Movies wishlist: downloading / importing / import failed (v0.126.0; not with Settings > Content = scenes)
    ]);
    const films = (filmsData && filmsData.rows) || [];
    // Movies sit in the same sections as scenes (v0.126.1): downloading / importing, source file missing (the download left no file to
    // import), import failed. Stuck and Needs review follow with the automatic movie search.
    const fileMissing = (f) => f.status === 'grabbed' && /^Downloaded file not found/.test(f.import_error || '');
    const filmsMissing = films.filter(fileMissing);
    const filmsDownloading = films.filter((f) => f.status === 'grabbed' && !fileMissing(f));
    const filmsFailed = films.filter((f) => f.status === 'import_failed');
    const filmsReview = films.filter((f) => (f.status === 'wanted' || f.upgrade) && f.review_candidate); // the automatic search found something to decide on (an owned movie: a better version)
    const filmsStuck = films.filter((f) => f.status === 'wanted' && !f.review_candidate);   // searched, nothing usable
    // Settings > Schedule & Import: may the fingerprints of a confirmed file be submitted to StashDB?
    uploadEnabled = !!appSettings && appSettings.fingerprint_upload_enabled === 'true';
    const firstError = responses.find((r) => r.error);
    if (firstError) {
      wrap.textContent = `Error: ${firstError.error}`;
      show();
      return;
    }
    if (mySeq !== renderSeq) return;
    const [sent, failed] = responses.map((r) => r.rows);

    const sentGrouped = window.SRW.groupByScene(sent);
    await window.SRW.fillCast(sentGrouped); // the cast next to the studio on the Stuck / wishlist rows
    // Only releases with an actual download job -- the rest of
    // 'sent_to_whisparr' is either still searching (nothing to show yet)
    // or "stuck" below (see the Releases page's own "On Wishlist"
    // section for the broader queued/searching view).
    const downloading = sentGrouped.filter((r) => r.download_job_id);
    listedDownloadIds = new Set(downloading.map((r) => r.scene_stashdb_id));
    // One-file movies carry their progress under "film:<id>" in the same live map (api/routers/releases.py download_progress()).
    films.filter((f) => f.status === 'grabbed' && f.mode === 'file').forEach((f) => listedDownloadIds.add(f.key));
    // "Stuck" (2026-09-21, user report -- a real release, "All Natural
    // Beauty Melanie Marie Creampie", sat here with a real
    // `last_search_result` of 'no_quality_match' and was invisible on
    // both the Calendar -- see releases.js's own isStuck()/pill-color
    // fix -- and here, since neither "Downloading" nor "Import failed"
    // covered it): searched at least once, nothing usable found, so it
    // won't progress on its own without either a config change or a
    // new indexer result appearing.
    // A scene dated today or yesterday is not "stuck" yet (v0.246.16): indexers post a release hours, sometimes a day, after its date. Same rule as the
    // sidecar's "Search all stuck" (main.py too_new_to_be_stuck); it stays on the Releases page's wishlist and is searched as usual.
    const day = (offset) => new Date(Date.now() - offset * 86400000).toISOString().slice(0, 10);
    const tooNew = (r) => !!r.scene_date && String(r.scene_date).slice(0, 10) >= day(1);
    const searchedNoJob = sentGrouped.filter((r) => !r.download_job_id && r.last_search_result);
    const stuck = searchedNoJob.filter((r) => !tooNew(r));
    const stuckTooNew = searchedNoJob.length - stuck.length;
    // "Source file missing" (2026-09-23, see file_importer.py's own
    // 'source_missing' failure path / project_roadmap_todo.md memory's
    // "Import-failed reason + conditional Retry" entry) -- deliberately
    // a SEPARATE section from "Stuck" above, not folded in: this is a
    // download that WAS grabbed (or is import-pending) but whose file
    // couldn't be found on disk when this sidecar tried to import it,
    // a different condition from "searched, nothing usable found".
    // Status stays 'sent_to_whisparr' on purpose (see
    // release_repo.set_import_error_for_scene()'s own docstring) so the
    // existing automatic per-cycle retry keeps running -- this section
    // only makes that previously-silent condition visible, it's not a
    // dead end needing a Retry button of its own.
    const sourceMissing = sentGrouped.filter((r) => !r.download_job_id && r.import_error_code === 'source_missing');

    // Every open "possible match" decision in one place (2026-09-28, user
    // request) -- before: wishlist ones only inside the Releases page's
    // collapsed "On Wishlist" rows, upgrade ones nowhere at all.
    const progressElements = new Map();
    // Built side by side (each one asks the sidecar for its own bits), the downloads first in the list.
    const stuckSection = (async () => {
      const [scenesPreview, moviesPreview, torrents] = await stuckExtras;
      const stuckGroup = withTorrentProblems(withStuckToolbar(await buildSection('Stuck (searched, nothing usable found)', stuck, {
        emptyText: 'Nothing stuck right now.',
        showSearchStatus: true, showSearchActions: true, showImportError: true, sortBy: 'last_search_at', onChanged: () => render(container),
        movieRows: movieRows(filmsStuck, progressElements, container),
      }), scenesPreview, moviesPreview), container, torrents);
      if (stuckTooNew) {
        const note = window.SRW.el('p', { className: 'srw-hint', textContent: `${stuckTooNew} scene${stuckTooNew === 1 ? '' : 's'} dated today or yesterday ${stuckTooNew === 1 ? 'is' : 'are'} not listed here yet: indexers often post a release hours after its date. ${stuckTooNew === 1 ? 'It is' : 'They are'} searched as usual and stay on the Releases page's wishlist.` });
        const summary = stuckGroup.querySelector(':scope > summary');
        if (summary) summary.after(note); else stuckGroup.prepend(note);
      }
      return stuckGroup;
    })();
    const sectionPromises = [
      ['review', 'Needs review', buildNeedsReview(reviews, container, filmsReview)],
      ['downloading', 'Downloading & importing', buildSection('Downloading & importing', downloading, {
        emptyText: 'Nothing downloading or importing right now.',
        showProgress: true, progressElements, movieRows: movieRows(filmsDownloading, progressElements, container),
      })],
      ['stuck', 'Stuck', stuckSection],
      ['missing', 'Source file missing', buildSection('Source file missing (still retrying automatically)', sourceMissing, {
        emptyText: 'Nothing missing right now.',
        showImportError: true, showSearchActions: true, sortBy: 'discovered_at', onChanged: () => render(container),
        movieRows: movieRows(filmsMissing, progressElements, container),
      })],
      ['failed', 'Import failed', buildSection('Import failed', window.SRW.groupByScene(failed), {
        emptyText: 'No failed imports.',
        showImportError: true, showFixWrongDownload: true, sortBy: 'discovered_at', onChanged: () => render(container),
        movieRows: movieRows(filmsFailed, progressElements, container),
      })],
    ];
    const sections = await Promise.all(sectionPromises.map(async ([key, short, group]) => [key, short, await group]));
    // The sections are tabs (v0.102.2), like on the Releases page: [Needs review (4)] [Downloading & importing (0)] ...
    // Their long headings stay on the sections themselves (hidden by CSS) and carry the live numbers into the tab labels.
    sections.forEach(([, , g]) => { g.open = true; g.classList.add('srw-panel-flat'); });
    wrap.classList.add('srw-activity-tabbed');
    wrap.appendChild(window.SRW.buildPaneTabs(
      sections.map(([key, short, g]) => ({ key, label: () => window.SRW.sectionTabLabel(g, short), pane: g, watch: g.querySelector('summary') })),
      'srw:activity-section',
    ));
    sections.forEach(([, , g]) => wrap.appendChild(g));

    if (mySeq !== renderSeq) return;
    show();

    // Pushed by the sidecar since v0.78.0 (was: polled every 5/10 s, user
    // report 2026-09-26: a download started while the page was open only
    // showed up after a reload): progress bars update in place, any
    // release change re-renders. Without a live connection the old
    // polling runs instead (5 s while something's running, else 10 s).
    liveDetach = window.SRW.attachLive(hint, {
      onProgress: (data) => applyProgressData(progressElements, container, data),
      // (a search panel the user has open stays until it is closed, see whenNoOpenPanel)
      onChange: () => window.SRW.whenNoOpenPanel(container, () => render(container)),
      onResync: () => window.SRW.whenNoOpenPanel(container, () => render(container)),
      poll: () => updateDownloadProgress(progressElements, container),
      pollMs: progressElements.size > 0 ? 5000 : 10000,
      hint,
    });
    if (previousDetach) previousDetach();
    // The status asked for at the start goes on the rows now; coming back to the tab asks again (a hidden tab gets no pushes).
    refreshProgress = () => updateDownloadProgress(progressElements, container);
    progressRequest.then((data) => {
      if (data && !data.error && mySeq === renderSeq) applyProgressData(progressElements, container, data);
    });
  }

  // "Search all stuck scenes / movies" (v0.201.0; split in two v0.224.0): ONE targeted indexer query for every stuck scene release, or the movie
  // search for every stuck movie, once, on request (the normal modes only ask a release daily in its first week, then weekly). The sidecar lists
  // what it would query first (dry run) and the click confirms it.
  function withStuckToolbar(group, scenes, movies) {
    const { el, confirmInline } = window.SRW;
    const nScenes = scenes && !scenes.error ? scenes.count || 0 : 0;
    const nMovies = movies && !movies.error ? movies.count || 0 : 0;
    if (!nScenes && !nMovies) return group;
    const status = el('span', { className: 'srw-key-status' });
    const buttons = [];
    if (nScenes) {
      const btn = el('button', {
        type: 'button', className: 'srw-btn', textContent: `Search all stuck scenes (${nScenes})`,
        title: 'One targeted indexer query per stuck scene release, right now. Releases already found to be duplicates are left out.',
      });
      btn.addEventListener('click', () => confirmInline(btn, `Send ${nScenes} indexer queries now, one per stuck scene release?`, async () => {
        status.textContent = ' starting...';
        try {
          const r = await window.SRW.runOp('indexer_search_run_stuck');
          status.textContent = r.error ? ` error: ${r.error}`
            : (r.started ? ` running in the background (${r.count} scene release(s)) -- the result appears in History; reload this page afterwards` : ' nothing to search');
        } catch (err) { status.textContent = ` error: ${err.message}`; }
      }));
      buttons.push(btn);
    }
    if (nMovies) {
      const btn = el('button', {
        type: 'button', className: 'srw-btn', textContent: `Search all stuck movies (${nMovies})`,
        title: 'The movie search (title + year, title, studio + title) for every stuck movie, right now. A plausible release becomes a candidate in Needs review; nothing is downloaded.',
      });
      btn.addEventListener('click', () => confirmInline(btn, `Send up to ${movies.max_queries} indexer queries now (the movie search, ${nMovies} movies)?`, async () => {
        status.textContent = ' starting...';
        try {
          const r = await window.SRW.runOp('films_search_stuck');
          status.textContent = r.error ? ` error: ${r.error}`
            : (r.started ? ` running in the background (${r.count} movie(s)) -- the result appears in History; reload this page afterwards` : (r.reason || ' nothing to search'));
        } catch (err) { status.textContent = ` error: ${err.message}`; }
      }));
      buttons.push(btn);
    }
    const bar = el('div', { className: 'srw-toolbar' }, [...buttons, status]);
    const summary = group.querySelector(':scope > summary');
    if (summary) summary.after(bar); else group.prepend(bar);
    return group;
  }

  // Torrents that need a look (v0.231.0, torrent_health.py): stalled, stuck, in error or gone from the download client. They sit at the top of
  // the Stuck tab (and count in its label) and go back to "Downloading" by themselves when they move again.
  const HEALTH_LABELS = { stalled: 'Stalled', stuck: 'Stuck', error: 'Error', missing: 'Missing' };

  async function withTorrentProblems(group, container, data) {
    const { el, confirmInline } = window.SRW;
    const rows = data && !data.error ? data.rows || [] : [];
    if (!rows.length) return group;
    const summary = group.querySelector(':scope > summary');
    if (summary) { // the tab label reads the number after the dash
      const m = summary.textContent.match(/^(.*—\s*)(\d+)\s*$/);
      if (m) summary.textContent = `${m[1]}${Number(m[2]) + rows.length}`;
    }
    const caps = await window.SRW.getSearchCapabilities().catch(() => null);
    const sources = caps && !caps.error ? caps.manual_sources || [] : [];
    const list = el('ul', { className: 'srw-torrent-problems' });
    const status = el('span', { className: 'srw-key-status' });
    const refresh = () => render(container);
    const act = (btn, mode, row, ask) => {
      const run = async () => {
        window.SRW.setBusy(status, 'working...');
        try {
          const r = await window.SRW.runOp(mode, { client_id: row.client_id, job_id: row.job_id });
          if (r && r.error) { status.textContent = ` error: ${r.error}`; return; }
          if (r && r.switched === false) { status.textContent = ` ${r.message || 'nothing found'}`; return; }
          if (mode === 'torrents_check_now') { status.textContent = r.started ? ' checking in the background -- reload this page in a minute' : ` ${r.reason || ''}`; return; }
          refresh();
        } catch (err) { status.textContent = ` error: ${err.message}`; }
      };
      btn.addEventListener('click', (e) => (ask ? confirmInline(e.currentTarget, ask, run) : run()));
      return btn;
    };
    rows.forEach((row) => {
      const swarm = [row.seeds != null ? `${row.seeds} seeds` : null, row.peers != null ? `${row.peers} peers` : null,
        row.availability != null && row.availability >= 0 ? `availability ${Number(row.availability).toFixed(2)}` : null].filter(Boolean).join(', ');
      const scene = row.kind === 'scene';
      const buttons = [];
      if (scene) {
        buttons.push(act(el('button', { type: 'button', className: 'srw-btn', textContent: 'Find another', title: 'Search for another torrent now; the old one is removed once a replacement is running' }),
          'torrents_switch', row, `Look for another torrent for "${row.title}" and, when one is found, remove this one with its data?`));
        if (sources.length) {
          const scn = { provider: row.provider, scene_stashdb_id: row.scene_id, title: row.title };
          buttons.push(window.SRW.buildManualSearch(scn, sources, caps.backend, refresh, {
            host: () => li,
            grab: (candidate) => window.SRW.runOp('releases_manual_search_grab', { provider: row.provider, scene_stashdb_id: row.scene_id, candidate, replace: true }),
          }));
        }
      }
      if (row.state === 'missing' || row.state === 'error') {
        buttons.push(act(el('button', { type: 'button', className: 'srw-btn', textContent: 'Re-add', title: 'Put the same torrent into the download client again' }), 'torrents_readd', row));
      }
      if (row.state !== 'stalled') buttons.push(act(el('button', { type: 'button', className: 'srw-btn', textContent: 'Keep waiting', title: 'No automatic switching for a day' }), 'torrents_keep', row));
      buttons.push(act(el('button', { type: 'button', className: 'srw-btn srw-btn-danger', textContent: 'Remove', title: 'Remove the torrent with its data, never use it again, and search again' }),
        'torrents_remove', row, `Remove this torrent with its downloaded data${scene ? '' : ' and put the movie back on the wishlist'}?`));
      const li = el('li', { className: `srw-torrent-problem srw-torrent-${row.state}` }, [
        el('span', { className: 'srw-review-warning', textContent: HEALTH_LABELS[row.state] || row.state }),
        el('span', { className: 'srw-release-title', textContent: row.title || '(untitled)' }),
        el('span', { className: 'srw-review-candidate-meta', textContent: `${row.percent}%${swarm ? ` — ${swarm}` : ''} — ${row.detail || row.raw_state || ''}` }),
        ...buttons,
      ]);
      list.appendChild(li);
    });
    const bar = el('div', { className: 'srw-toolbar' }, [
      el('strong', { textContent: `Torrents needing a look (${rows.length})` }),
      act(el('button', { type: 'button', className: 'srw-btn', textContent: 'Check now', title: 'Run the torrent check right now (normally every 30 minutes)' }), 'torrents_check_now', {}),
      status,
    ]);
    const anchor = group.querySelector(':scope > .srw-toolbar') || summary;
    const block = el('div', { className: 'srw-torrent-block' }, [bar, list]);
    if (anchor) anchor.after(block); else group.prepend(block);
    return group;
  }

  async function buildSection(label, releases, {
    emptyText, showProgress, progressElements, showSearchStatus, sortBy, collapsed,
    showImportError, showFixWrongDownload, onChanged, showThumb = true, rememberKey, showSearchActions, movieRows = [], openScene = false,
  } = {}) {
    const {
      el, sceneUrl, buildEntityIcons, buildLocalEntityLink, buildReleaseThumb, buildStudioContext, confirmInline,
    } = window.SRW;
    // Movies of the Movies wishlist are items of the SAME list as the scenes (v0.128.4): one filter bar, one pager, one sort order.
    // `movieRows` = the finished <li> of movieRows() below, each carrying its movie in `li._film`.
    const movieItems = movieRows.map((li) => {
      const f = li._film || {};
      return { _filmLi: li, title: f.title, studio_name: f.studio_name || '', scene_date: (f.release_date || '').slice(0, 10),
               _when: f.imported_at || f.last_search_at || f.grabbed_at || '' };
    });
    const sorted = (sortBy
      ? [...releases, ...movieItems].sort((a, b) => ((b[sortBy] || b._when || '')).localeCompare((a[sortBy] || a._when || '')))
      : [...releases, ...movieItems]);
    const group = el('details', { className: 'srw-date-group' }, [
      el('summary', { textContent: `${label} — ${sorted.length}` }),
    ]);
    // Open/closed remembered per browser (2026-09-25), `collapsed` is
    // only the default until the user toggles it -- see core.js's
    // rememberOpen().
    window.SRW.rememberOpen(group, rememberKey || `activity:${label}`, !collapsed);
    if (sorted.length === 0) {
      group.appendChild(el('p', { className: 'srw-hint', textContent: emptyText || 'Nothing here.' }));
      return group;
    }
    // Paged at 40 per page (2026-09-25, user request -- "Imported" only
    // ever grows); with the shared filter bar since 2026-09-26. Rows are
    // only built for the visible page.
    function renderRow(r) {
      if (r._filmLi) return r._filmLi; // a movie
      const li = [
        showThumb ? buildReleaseThumb(r) : null,
        buildEntityIcons(r.entity_types),
        buildLocalEntityLink(r),
        buildStudioContext(r),
        el('a', {
          className: 'srw-release-title', href: sceneUrl(r.provider, r.scene_stashdb_id),
          target: '_blank', rel: 'noopener noreferrer', textContent: r.title || '(untitled)',
        }),
        el('span', { className: 'srw-film-class srw-film-class-likely', textContent: 'Scene' }), // like the Movie pill
      ].filter(Boolean);
      if (openScene) { // the scene in Stash: the link is set once the page's local scene ids are known (resolveOpenScenes)
        li.push(window.SRW.iconButton('a', 'faPlay', 'Open scene', { className: 'srw-btn srw-open-scene', target: '_blank', rel: 'noopener noreferrer', hidden: true,
          'data-provider': r.provider, 'data-scene': r.scene_stashdb_id, title: 'Open this scene in Stash (new tab)' }));
      }
      // The scene's release date comes first in the row (arrangeReleaseRow); this timestamp says what happened when.
      const when = r.imported_at || r.handoff_at || r.discovered_at;
      const whenLabel = r.imported_at ? 'imported' : (r.handoff_at ? 'added' : 'found');
      li.push(el('span', { className: 'srw-release-date', title: 'When this happened',
        textContent: when ? `${whenLabel} ${when.slice(0, 16).replace('T', ' ')}` : '' }));
      if (showSearchStatus) {
        li.push(el('span', {
          className: 'srw-search-status',
          title: r.last_search_at ? `Last checked: ${r.last_search_at.slice(0, 16).replace('T', ' ')}` : '',
          textContent: window.SRW.searchStatusLabel(r.download_job_id, r.last_search_result),
        }));
      }
      // The same search actions as on the Releases page's On Wishlist rows (v0.106.9): Last search (what was searched and why
      // nothing was taken), Search via Whisparr, and Search… (own query + Grab).
      if (showSearchActions) li.push(window.SRW.buildSearchActions(r, () => (onChanged ? onChanged() : null)));
      if (showSearchActions && r.status === 'sent_to_whisparr' && !r.download_job_id) li.push(window.SRW.buildWishlistDismiss(r, () => (onChanged ? onChanged() : null)));
      if (showProgress && r.download_job_id) {
        const fill = el('div', { className: 'srw-download-progress-fill' });
        const text = el('span', { className: 'srw-download-progress-text' });
        const wrap = el('span', { className: 'srw-download-progress' }, [
          el('div', { className: 'srw-download-progress-track' }, [fill]),
          text,
        ]);
        li.push(wrap);
        // A download waiting for the encode worker can be sent to the NAS instead (shown by applyProgress() only in that phase).
        const localBtn = el('button', {
          className: 'srw-btn', textContent: 'Encode on the NAS now', style: 'display:none',
          title: 'Do not wait for the encode worker: let the sidecar re-encode this download itself',
          onClick: async (e) => {
            e.currentTarget.disabled = true;
            try { await window.SRW.runOp('encode_run_local', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }); }
            catch (err) { window.SRW.notify('Encode on the NAS', err.message, 'error'); }
          },
        });
        li.push(localBtn);
        progressElements.set(r.scene_stashdb_id, { fill, text, wrap, localBtn });
      }
      // Persisted failure reason (2026-09-23) -- see
      // release_repo.set_import_error_for_scene(). Shown as plain text,
      // same spot searchStatusLabel occupies above, since the two never
      // appear in the same section.
      // Imported without fingerprint verification (2026-09-25): the
      // provider had no fingerprints for this scene yet; re-checked
      // automatically, see file_importer.recheck_unverified_imports().
      if (r.import_error_code === 'unverified') {
        li.push(el('span', {
          className: 'srw-unverified-badge', textContent: 'unverified',
          title: r.import_error_detail || 'Imported without fingerprint verification -- re-checked automatically',
        }));
      }
      if (showImportError && r.import_error_detail) {
        li.push(el('span', { className: 'srw-search-status', textContent: r.import_error_detail }));
        // When that download was grabbed (2026-09-26): a second wrong
        // download otherwise reads exactly like the first one.
        if (r.grabbed_at) {
          const at = new Date(`${r.grabbed_at.replace(' ', 'T')}Z`);
          li.push(el('span', {
            className: 'srw-release-date', title: r.grabbed_title || '',
            textContent: `downloaded ${Number.isNaN(at.getTime()) ? r.grabbed_at : at.toLocaleString()}`,
          }));
        }
      } else if (showFixWrongDownload && r.status === 'import_failed' && !r.import_error_code) {
        // No stored reason (2026-09-26): failures from before v0.51
        // recorded why -- say so instead of showing nothing.
        li.push(el('span', {
          className: 'srw-search-status',
          textContent: 'Failed before failure reasons were recorded -- search again or dismiss',
        }));
      }
      // Look at the downloaded video before deciding (v0.106.4): the scene Stash created from the imported file, as a real link
      // (new tab; never blocked like a popup). The scene is looked up when the row is drawn; no file in Stash -> no link.
      if (r.status === 'import_failed' && r.pending_import_path) {
        const openLink = window.SRW.iconButton('a', 'faPlay', 'Open file in Stash', {
          target: '_blank', rel: 'noopener noreferrer', hidden: true,
          title: 'Open the downloaded file (the scene Stash created from it) in a new tab to play it',
        });
        window.SRW.runOp('releases_imported_file', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }).then((found) => {
          if (found && found.scene_id) { openLink.href = `/scenes/${found.scene_id}`; openLink.hidden = false; }
        }).catch(() => {});
        li.push(openLink);
      }
      // Generic actions for failures without an automatic fix (2026-09-26):
      // back on the wishlist for a new search, or dismiss.
      if (showFixWrongDownload && r.status === 'import_failed' && r.import_error_code !== 'fingerprint_mismatch') {
        const actStatus = el('span', { className: 'srw-key-status' });
        if (r.import_error_code === 'encode_failed') { // the download is still in place: encode it again instead of fetching a new one
          li.push(window.SRW.iconButton('button', ['faRotateRight', 'faFilm'], 'Retry the encode', {
            className: 'srw-btn srw-btn-primary',
            title: 'The downloaded file is still there: queue the re-encode again (it goes to the encode worker or the NAS as set in Settings > Quality)',
            onClick: async () => {
              const result = await window.SRW.runOp('releases_retry_encode', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id });
              if (result.error) { actStatus.textContent = ` error: ${result.error}`; return; }
              window.SRW.notify('Retry the encode', `"${r.title}" is queued again.`, 'success');
              if (onChanged) onChanged();
            },
          }));
        }
        if (r.import_error_code !== 'scene_gone') {
          li.push(window.SRW.iconButton('button', 'faRotateRight', 'Search again', {
            className: 'srw-btn srw-btn-primary',
            title: 'Back on the wishlist -- the next search run looks for it again',
            onClick: async () => {
              const result = await window.SRW.runOp('releases_retry_failed', {
                provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
              });
              if (result.error) { actStatus.textContent = ` error: ${result.error}`; return; }
              window.SRW.notify('Search again', `"${r.title}" is back on the wishlist.`, 'success');
              if (onChanged) onChanged();
            },
          }));
        }
        li.push(el('button', {
          className: 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss', textContent: 'Dismiss',
          onClick: async () => {
            const result = await window.SRW.runOp('releases_dismiss', {
              provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
            });
            if (result && result.error) { actStatus.textContent = ` error: ${result.error}`; return; }
            window.SRW.releaseDismissedNotice(r, result, () => { if (onChanged) onChanged(); });
            if (onChanged) onChanged();
          },
        }));
        li.push(actStatus);
      }
      // "Fix wrong download" retry (2026-09-23) -- ONLY for
      // 'fingerprint_mismatch' (a release this sidecar itself imported
      // that turned out not to match StashDB's own fingerprints).
      // 'scene_gone' gets no retry at all (permanent, nothing to fix);
      // 'source_missing' never reaches this section in the first place
      // (see render()'s own section split above). See
      // routers/releases.py's fix_wrong_download() for the full
      // sequence this triggers.
      if (showFixWrongDownload && r.import_error_code === 'fingerprint_mismatch') {
        const fixStatus = el('span', { className: 'srw-key-status' });
        const fixBtn = el('button', {
          className: 'srw-btn srw-btn-primary', textContent: 'Fix wrong download',
          onClick: (e) => {
            confirmInline(e.currentTarget,
              `Move the wrong file for "${r.title}" to the recycle bin, blocklist that download, and put the scene back on the wishlist for a new search?`,
              async () => {
                fixStatus.textContent = ' fixing...';
                try {
                  const result = await window.SRW.runOp('releases_fix_wrong_download', {
                    provider: r.provider, entity_type: r.entity_type, entity_stashdb_id: r.entity_stashdb_id,
                    scene_stashdb_id: r.scene_stashdb_id,
                  });
                  if (result.error) { fixStatus.textContent = ` error: ${result.error}`; return; }
                  if (onChanged) onChanged();
                } catch (err) {
                  fixStatus.textContent = ` error: ${err.message}`;
                }
              },
              { danger: true },
            );
          },
        });
        // Re-check fingerprints (v0.235.0): somebody may have submitted fingerprints to the provider since the download was judged.
        const recheckBtn = window.SRW.iconButton('button', 'faFingerprint', 'Re-check fingerprints', {
          title: 'Ask the provider for the scene\'s fingerprints again and compare them with the file: a match imports it, otherwise it tells how close the file is',
          onClick: async (e) => {
            const button = e.currentTarget;
            button.disabled = true;
            window.SRW.setBusy(fixStatus, 'checking...');
            try {
              const result = await window.SRW.runOp('releases_recheck_fingerprints', {
                provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
              });
              if (result.error) { fixStatus.textContent = ` error: ${result.error}`; window.SRW.notify('Re-check fingerprints', result.error, 'error'); return; }
              fixStatus.textContent = ` ${result.message}`;
              if (result.result === 'verified') {
                window.SRW.notify('Re-check fingerprints', `"${r.title}": ${result.message}`, 'success');
                if (onChanged) onChanged();
              }
            } catch (err) {
              fixStatus.textContent = ` error: ${err.message}`;
            } finally {
              button.disabled = false;
            }
          },
        });
        const confirmBtn = window.SRW.iconButton('button', 'faCheck', 'File is correct', {
          title: 'The video IS this scene (e.g. a re-cut nobody registered a fingerprint for): keep the file and count it as imported',
          onClick: (e) => confirmInline(e.currentTarget,
            `Keep the file for "${r.title}" although it doesn't match the fingerprints? The scene gets this release's metadata and counts as imported.`,
            async () => {
              fixStatus.textContent = ' applying...';
              const result = await window.SRW.runOp('releases_confirm_file', {
                provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
              });
              if (result.error) { fixStatus.textContent = ` error: ${result.error}`; window.SRW.notify('File is correct', result.error, 'error'); return; }
              window.SRW.notify('File is correct', `"${r.title}" is imported (marked as confirmed by you).`, 'success');
              if (onChanged) onChanged();
            }),
        });
        // + Upload (v0.106.7): the same, and afterwards the file's fingerprints go to StashDB (public). StashDB releases only; hidden
        // while Settings > Schedule & Import > "Allow submitting fingerprints" is off.
        let uploadBtn = null;
        if (r.provider === 'stashdb' && uploadEnabled) { // hidden while submitting is off (Settings > Schedule & Import)
          uploadBtn = window.SRW.iconButton('button', 'faCloudArrowUp', 'Correct + submit to StashDB', {
            title: 'Like "File is correct", and the fingerprints of the file are submitted to StashDB (public, cannot be undone)',
            onClick: (e) => confirmInline(e.currentTarget,
              `Keep the file for "${r.title}" AND submit its fingerprints to StashDB? That is public and cannot be undone.`,
              async () => {
                fixStatus.textContent = ' applying...';
                const result = await window.SRW.runOp('releases_confirm_file', {
                  provider: r.provider, scene_stashdb_id: r.scene_stashdb_id, submit: true,
                });
                if (result.error) { fixStatus.textContent = ` error: ${result.error}`; window.SRW.notify('File is correct', result.error, 'error'); return; }
                if (result.submit_error) window.SRW.notify('Submit to StashDB', `The file is confirmed, but the submission failed: ${result.submit_error}`, 'error');
                else window.SRW.notify('File is correct', `"${r.title}" is imported and its fingerprints were submitted to StashDB.`, 'success');
                if (onChanged) onChanged();
              }, { danger: true }),
          });
        }
        li.push(recheckBtn, confirmBtn, ...(uploadBtn ? [uploadBtn] : []), fixBtn, fixStatus);
      }
      return window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item', 'data-scene-id': r.scene_stashdb_id }, li), r);
    }
    // Filter bar + paging (2026-09-26, user principle), same component as
    // everywhere else; the bar shows once a section has >10 entries.
    const view = window.SRW.buildFilteredPagedList({
      filters: ['search', 'kind', 'description', 'type', 'vr', 'dates', 'providers', 'upgrades'],
      perPage: 40,
      showFilterBarFrom: 10,
      fetchPage: window.SRW.localPageFetcher(() => sorted, {
        text: (r) => `${r.title || ''} ${r.studio_name || ''}`,
        description: (r) => r.details,
        type: (r) => (r.entity_types && r.entity_types.length === 1 ? r.entity_types[0] : r.entity_type),
        vr: (r) => r.is_vr,
        upgrade: (r) => Boolean(r.upgrade_of),
        provider: (r) => r.provider,
        date: (r) => r.scene_date || r.scene_created,
        kind: (r) => (r._filmLi ? 'movies' : 'scenes'),
      }),
      renderItems: (items) => {
        const ul = el('ul', { className: 'srw-release-list' }, items.map(renderRow));
        if (openScene) resolveOpenScenes(ul);
        return ul;
      },
    });
    group.appendChild(view.node);
    await view.reset(); // rows (and their progress bars) exist before the poll below starts
    return group;
  }

  // One request for the page: which of its scenes are in the library -> "Open scene" gets its link (scenes not in Stash keep it hidden).
  function resolveOpenScenes(ul) { return window.SRW.resolveSceneLinks(ul); }

  // Movie rows for the Activity sections (v0.126.0/.1): the Movies wishlist entries that are downloading / importing / missing a file /
  // failed, in the same rows-with-a-bar look as the scenes. Each row can open the list of its files as Stash scenes (thumbnail + link) to
  // check them. The full movie tools stay on the Releases page > Movies.
  function movieRows(films, progressElements, container, { thumb = true } = {}) {
    const { el, notify, confirmInline } = window.SRW;
    return films.map((f) => {
      const slot = el('div', { className: 'srw-rr-slot' }); // the opened scene list: under the whole row (the row is a grid)
      const children = [
        thumb ? movieThumb(f) : null,
        el('span', { className: 'srw-release-title', textContent: `${f.title}${f.year ? ` (${f.year})` : ''}` }),
        el('span', { className: 'srw-film-class srw-film-class-sure', textContent: 'Movie' }),
      ].filter(Boolean);
      if (f.status === 'grabbed' && f.mode === 'file' && !/^Downloaded file not found/.test(f.import_error || '')) {
        const fill = el('div', { className: 'srw-download-progress-fill' });
        const text = el('span', { className: 'srw-download-progress-text' });
        const wrap = el('span', { className: 'srw-download-progress' }, [el('div', { className: 'srw-download-progress-track' }, [fill]), text]);
        children.push(wrap);
        progressElements.set(f.key, { fill, text, wrap });
      } else if (f.status === 'grabbed' && f.mode === 'scenes') {
        children.push(el('span', { className: 'srw-hint', textContent: `Put together from StashDB scenes: ${f.scenes_imported || 0} of ${f.scenes_total || 0} imported` }));
      }
      let searchNow = null; // joined with Manual below, like the buttons of a scene row (v0.243.4)
      if (f.status === 'wanted') { // searched by the automatic movie search without a usable result
        const why = { no_match: 'nothing usable found', rejected: 'the last candidate was rejected', error: 'the search failed' }[f.last_search_result] || 'searched';
        children.push(el('span', { className: 'srw-search-status', textContent: `${why}${f.last_search_at ? ` (${f.last_search_at.slice(0, 16)} UTC)` : ''}` }));
        searchNow = window.SRW.iconButton('button', 'faMagnifyingGlass', 'Search now', { title: 'Search now: ask the indexers for this movie again (1-3 requests)',
          onClick: async (e) => {
            e.currentTarget.disabled = true;
            window.SRW.busyIcon(e.currentTarget);
            const res = await window.SRW.runOp('films_search', { id: f.id });
            if (res.error) { notify('Movie search', res.error, 'error'); } else {
              notify('Movie search', res.result === 'needs_review' ? `Candidate found: ${res.candidate.title}` : (res.result === 'no_match' ? 'Nothing usable found.' : `Search failed: ${res.error || ''}`), res.result === 'needs_review' ? 'success' : 'info');
            }
            render(container);
          } });
      }
      // Own query + Grab (v0.211.1), the same panel as on the Movies page and for scenes; prefill "Title Year".
      let li = null;
      if (['wanted', 'import_failed'].includes(f.status)) {
        const searchSlot = el('span', { className: 'srw-search-actions' });
        window.SRW.getSearchCapabilities().then((caps) => {
          const sources = (caps && caps.manual_sources) || [];
          if (!sources.length) return;
          const asRelease = { provider: 'theporndb', scene_stashdb_id: f.tpdb_id || `film-${f.id}`, title: `${f.title}${f.year ? ` ${f.year}` : ''}` };
          searchSlot.appendChild(window.SRW.buildManualSearch(asRelease, sources, caps.backend, () => render(container), {
            host: () => li,
            grab: (candidate) => window.SRW.runOp('films_grab', { id: f.id, candidate }),
          }));
        });
        children.push(searchNow ? el('span', { className: 'srw-btn-group' }, [searchNow, searchSlot]) : searchSlot);
        searchNow = null;
      }
      if (searchNow) children.push(searchNow);
      if (f.status === 'imported' && f.imported_at) children.push(el('span', { className: 'srw-release-date', textContent: `imported ${f.imported_at.slice(0, 16)}` }));
      if (f.import_error) children.push(el('span', { className: 'srw-hint', textContent: f.import_error }));
      if ((f.scenes || []).length) {
        let open = false;
        const scenesBtn = window.SRW.iconButton('button', 'faList', `Scenes (${f.scenes.length})`, { className: 'srw-btn',
          title: `Scenes (${f.scenes.length}): open the files of this movie as Stash scenes to check that they belong together`,
          onClick: () => {
            open = !open;
            slot.replaceChildren(...(open ? [buildFilmScenes(f)] : []));
          } });
        scenesBtn.append(document.createTextNode(` ${f.scenes.length}`)); // the count stays visible
        children.push(scenesBtn);
      }
      if (f.stash_group_id) {
        children.push(window.SRW.iconButton('a', 'faPlay', 'Open group', { href: `/groups/${f.stash_group_id}`, target: '_blank', rel: 'noopener noreferrer', title: 'Open group: the movie\'s group in Stash (new tab)' }));
      }
      if (f.status === 'wanted') children.push(...window.SRW.buildFilmDismissButtons(f, () => render(container))); // Stuck: get rid of a movie / group nobody wants
      if (f.status === 'import_failed') {
        children.push(window.SRW.iconButton('button', ['faLayerGroup', 'faExclamation'], 'Group anyway', { className: 'srw-btn',
          title: 'Group anyway: build the Group from the files as they are, although the length does not fit',
          onClick: (e) => confirmInline(e.currentTarget, `Build the Group for "${f.title}" regardless of the length?`, async () => {
            const res = await window.SRW.runOp('films_finalize', { id: f.id, force: true });
            if (res.error) { notify('Movie', res.error, 'error'); return; }
            notify('Movie', `"${f.title}": ${res.status || 'done'}`, 'success');
            render(container);
          }) }));
      }
      if (f.status === 'import_failed' && f.has_files) { // the wrong movie (e.g. another part of a series): remove it again
        const discard = (dismiss) => async () => {
          const res = await window.SRW.runOp('films_discard', { id: f.id, dismiss });
          if (res.error) { notify('Movie', res.error, 'error'); return; }
          notify('Movie', `"${f.title}": ${res.files.length} file(s) moved to the recycle bin${res.scenes.length ? `, ${res.scenes.length} Stash scene(s) removed` : ''}; `
            + (dismiss ? 'dismissed.' : 'the release is blocklisted for this movie and it is searched again.'), 'success');
          render(container);
        };
        children.push(el('button', { className: 'btn btn-outline-danger srw-btn-fit', textContent: 'Delete + search',
          title: 'The downloaded file(s) go to the recycle bin, their Stash scene(s) are removed, this release is never proposed again for this movie, and it is searched again',
          onClick: (e) => confirmInline(e.currentTarget, `Delete the downloaded file(s) of "${f.title}" (recycle bin) and search again?`, discard(false), { danger: true }) }));
        children.push(el('button', { className: 'btn btn-danger srw-btn-fit', textContent: 'Delete + dismiss',
          title: 'The downloaded file(s) go to the recycle bin, their Stash scene(s) are removed, and the movie is dismissed from the wishlist',
          onClick: (e) => confirmInline(e.currentTarget, `Delete the downloaded file(s) of "${f.title}" (recycle bin) and dismiss the movie?`, discard(true), { danger: true }) }));
      }
      // Same row layout as the scenes (cover | title + pill + meta | buttons); the opened scene list goes below the row.
      li = window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, children), { scene_date: (f.release_date || '').slice(0, 10) });
      li.appendChild(slot);
      li._film = f;
      return li;
    });
  }

  // The movie's cover (ThePornDB poster), same look as the scene thumbnails; links to the Group when it exists. A blocked/missing image leaves nothing behind.
  function movieThumb(f) {
    if (!f.image_url) return null;
    const { el } = window.SRW;
    const img = el('img', { className: 'srw-release-thumb srw-movie-cover', src: f.image_url, alt: '', loading: 'lazy' });
    // The cover opens the movie's Group in Stash; a movie without a Group (nothing was grouped yet) has nothing to open and says so (v0.243.16).
    const wrap = f.stash_group_id
      ? el('a', { className: 'srw-release-thumb-link', href: `/groups/${f.stash_group_id}`, target: '_blank', rel: 'noopener noreferrer', title: 'Open the group in Stash (new tab)' }, [img])
      : el('span', { className: 'srw-release-thumb-link', title: 'No group in Stash yet' }, [img]);
    img.addEventListener('error', () => wrap.remove());
    return wrap;
  }

  // The files of one movie as Stash scenes: thumbnail + link (new tab), length, and the Group each is in.
  function buildFilmScenes(f) {
    const { el } = window.SRW;
    const total = f.scenes.reduce((n, sc) => n + (sc.minutes || 0), 0);
    return el('div', { className: 'srw-manual-search-panel' }, [
      el('p', { className: 'srw-hint', textContent: `${f.scenes.length} file(s), ${Math.round(total)} min in total. Open a scene (new tab) to check that it belongs to this movie.` }),
      ...f.scenes.map((sc) => el('div', { className: 'srw-manual-search-result srw-group-scene' }, [
        sc.scene_id
          ? el('a', { className: 'srw-vr-thumb-link', href: `/scenes/${sc.scene_id}`, target: '_blank', rel: 'noopener noreferrer', title: 'Open the scene in Stash (new tab)' },
            [el('img', { className: 'srw-vr-thumb', src: `/scene/${sc.scene_id}/screenshot`, loading: 'lazy', alt: '' })])
          : el('span', { className: 'srw-hint', textContent: 'no scene yet' }),
        el('div', {}, [
          sc.scene_id ? el('a', { className: 'srw-release-title', href: `/scenes/${sc.scene_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: sc.title || sc.file })
            : el('span', { className: 'srw-release-title', textContent: sc.file }),
          el('div', { className: 'srw-hint', textContent: `${sc.file}${sc.minutes ? ` — ${sc.minutes} min` : ''}${(sc.groups || []).length ? ` — in group ${sc.groups.map((g) => g.name).join(', ')}` : ' — no group'}` }),
        ]),
      ])),
    ]);
  }

  // Same polling shape as the Releases page's own updateDownloadProgress()
  // (stash-release-manager-releases.js) -- not centralized since it's
  // the only piece of logic the two pages would share, and each page's
  // own `progressElements` map/timer variable already needs to live in
  // its own closure regardless.
  async function updateDownloadProgress(progressElements, container) {
    let data;
    try {
      data = await window.SRW.runOp('releases_download_progress');
    } catch (e) {
      return;
    }
    if (!data || data.error) return;
    applyProgressData(progressElements, container, data);
  }

  // Shared by the pushed 'progress' events and the fallback poll.
  function applyProgressData(progressElements, container, data) {
    if (!container.querySelector('.srw-activity-page')) return; // page left
    // Keeps the header's running-count indicator right even when the live stream is unavailable (this page polls then).
    window.SRW.setActivityCount(Object.values(data || {}).filter(Boolean).length);
    // Download AND import phases since 2026-09-26, see core.js's applyProgress().
    progressElements.forEach((elements, sceneId) => {
      window.SRW.applyProgress(elements, data[sceneId]);
    });
    // Re-render when the set of active downloads/imports changed: a new
    // one appeared that isn't shown yet, or a shown one finished (it then
    // moves to "Imported" / "Import failed"). Open sections stay open
    // (rememberOpen).
    // Compared against ALL listed downloads, not just the rendered page of
    // them (the section is paged) -- otherwise page 2 would re-render forever.
    const active = new Set(Object.keys(data).filter((k) => data[k]));
    const shown = listedDownloadIds;
    const appeared = [...active].some((k) => !shown.has(k));
    const finished = [...shown].some((k) => !active.has(k));
    if (appeared || finished) render(container);
  }

  const UPGRADE_REASON_LABEL = {
    vr_2d: 'VR scene, 2D file', vr_mono: 'VR file is mono', vr_low_quality: 'weak VR quality',
    below_cutoff: 'below the quality cutoff',
  };

  function buildNeedsReview(reviews, container, filmsReview = []) {
    const { el, sceneUrl, releaseTitleNode, confirmInline } = window.SRW;
    const wishlist = (reviews && reviews.wishlist) || [];
    const upgrades = (reviews && reviews.upgrades) || [];
    const total = wishlist.length + upgrades.length + filmsReview.length;
    const group = el('details', { className: 'srw-date-group' }, [
      el('summary', { textContent: `Needs review — ${total}` }),
    ]);
    window.SRW.rememberOpen(group, 'activity:Needs review', true);
    if (reviews && reviews.error) {
      group.appendChild(el('p', { textContent: `Error: ${reviews.error}` }));
      return group;
    }
    if (!total) {
      group.appendChild(el('p', { className: 'srw-hint', textContent: 'No open decisions.' }));
      return group;
    }
    group.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'The search found a release that looks right but didn\'t clear the bar for an automatic grab. '
        + 'Accept downloads it, Reject blocklists it for this scene.',
    }));

    // `sceneDate` (wishlist rows): a candidate release that is much OLDER than the scene's own release date cannot be this scene.
    function candidateLine(c, sceneDate) {
      const facts = [c.indexer, c.protocol === 'torrent' ? `Torrent${c.seeders != null ? `, ${c.seeders} seeders` : ''}` : null,
        c.size ? `${(c.size / 1e9).toFixed(1)} GB` : null,
        c.age != null ? window.SRW.ageWords(c.age) : null].filter(Boolean).join(' · ');
      const warning = window.SRW.ageWarning(c.age, sceneDate);
      return el('div', { className: 'srw-review-box' }, [
        releaseTitleNode(c.title, c.infoUrl),
        facts ? el('span', { className: 'srw-release-date', textContent: facts }) : null,
        warning,
        c.reencode ? el('span', {
          className: 'srw-review-warning',
          title: 'Last-resort 2160p release: after the download it is scaled down to 1080p and re-encoded to H.265 before the import (Settings > Last resort for older scenes).',
          textContent: 'will be re-encoded to 1080p on accept',
        }) : null,
      ]);
    }

    // Cover of a scene row (v0.131.2): the release's own image (wishlist) or the Stash screenshot (upgrade); a missing image leaves nothing behind.
    function reviewThumb(src, href) {
      if (!src) return null;
      const img = el('img', { className: 'srw-release-thumb', src, alt: '', loading: 'lazy' });
      const wrap = href
        ? el('a', { className: 'srw-release-thumb-link', href, target: '_blank', rel: 'noopener noreferrer' }, [img])
        : el('span', { className: 'srw-release-thumb-link' }, [img]);
      img.addEventListener('error', () => wrap.remove());
      return wrap;
    }

    // One row: what the scene is + the candidate on the left, the decision on the right.
    function reviewRow(head, candidate, buttons, thumb = null) {
      return el('li', { className: `srw-release-item srw-review-row${thumb ? ' srw-review-row-thumb' : ''}` }, [
        thumb,
        el('div', { className: 'srw-review-info' }, [el('div', { className: 'srw-review-head' }, head), candidate]),
        el('div', { className: 'srw-review-actions' }, buttons),
      ]);
    }

    function decisionButtons(onAccept, onReject, title, onAcceptScene = null, onUndoReject = null) {
      const status = el('span', { className: 'srw-key-status' });
      const run = async (fn, label, undo = null) => {
        status.textContent = ` ${label}...`;
        try {
          const result = await fn();
          if (result && result.error) { status.textContent = ` error: ${result.error}`; return; }
          if (undo) window.SRW.dismissedNotice(`Rejected: ${title}`, 'Blocklisted for this scene.', undo, () => render(container));
          render(container);
        } catch (err) {
          status.textContent = ` error: ${err.message}`;
        }
      };
      return [
        el('button', {
          className: 'srw-btn srw-btn-primary', textContent: 'Accept',
          onClick: (e) => confirmInline(e.currentTarget, `Download "${title}"?`, () => run(onAccept, 'grabbing')),
        }),
        onAcceptScene ? el('button', {
          className: 'srw-btn', textContent: 'Accept as one scene',
          title: 'The file is only one scene (part) of this movie: it joins the movie\'s Group and the movie stays on the wishlist for the other parts',
          onClick: (e) => confirmInline(e.currentTarget, `Download "${title}" as ONE SCENE of the movie? The movie stays wanted.`, () => run(onAcceptScene, 'grabbing')),
        }) : null,
        el('button', {
          className: 'srw-btn', textContent: 'Reject',
          onClick: (e) => confirmInline(e.currentTarget, `Blocklist "${title}" for this scene?`,
            () => run(onReject, 'rejecting', onUndoReject), { danger: true }),
        }),
        status,
      ];
    }

    const ul = el('ul', { className: 'srw-release-list' });
    wishlist.forEach((r) => {
      const c = r.review_candidate || {};
      ul.appendChild(reviewRow([
        el('span', { className: 'srw-history-category', textContent: 'Wishlist' }),
        el('a', {
          className: 'srw-release-title', href: sceneUrl(r.provider, r.scene_stashdb_id),
          target: '_blank', rel: 'noopener noreferrer', textContent: r.title || '(untitled)',
        }),
        el('span', { className: 'srw-release-date', textContent: [r.studio_name, r.scene_date].filter(Boolean).join(' · ') }),
      ], candidateLine(c, r.scene_date), decisionButtons(
        () => window.SRW.runOp('releases_review_candidate_accept', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }),
        () => window.SRW.runOp('releases_review_candidate_reject', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }),
        c.title, null,
        () => window.SRW.runOp('releases_review_candidate_restore', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }),
      ), reviewThumb(r.image_url, sceneUrl(r.provider, r.scene_stashdb_id))));
    });
    filmsReview.forEach((f) => { // movies: the automatic movie search found a plausible release (Reject = never this release for this movie)
      const c = f.review_candidate || {};
      ul.appendChild(reviewRow([
        el('span', { className: 'srw-history-category', textContent: f.upgrade ? 'Upgrade · Movie' : 'Movie' }),
        el('span', { className: 'srw-release-title', textContent: `${f.title}${f.year ? ` (${f.year})` : ''}` }),
        f.upgrade ? el('span', { className: 'srw-release-date', textContent: `now ${f.owned_height || '?'}p -- accepting replaces the file (the old one goes to the recycle bin)` }) : null,
        f.parts_imported ? el('span', { className: 'srw-release-date', textContent: `${f.parts_imported} part(s) already in the group` }) : null,
      ], candidateLine(c), decisionButtons(
        () => window.SRW.runOp('films_review_accept', { id: f.id }),
        () => window.SRW.runOp('films_review_reject', { id: f.id }),
        c.title,
        () => window.SRW.runOp('films_review_accept', { id: f.id, as_scene: true }),
      ), movieThumb(f)));
    });
    upgrades.forEach((u) => {
      const c = u.review_candidate || {};
      const now = u.cur_width ? `now ${u.cur_width}x${u.cur_height}` : '';
      ul.appendChild(reviewRow([
        el('span', {
          className: 'srw-history-category',
          textContent: `Upgrade · ${UPGRADE_REASON_LABEL[u.reason] || u.reason}`,
        }),
        el('a', {
          className: 'srw-release-title', href: `/scenes/${u.scene_id}`,
          target: '_blank', rel: 'noopener noreferrer', textContent: u.scene_title || '(untitled)',
        }),
        now ? el('span', { className: 'srw-release-date', textContent: now }) : null,
      ], candidateLine(c), decisionButtons(
        () => window.SRW.runOp('upgrades_review_accept', { id: u.id }),
        () => window.SRW.runOp('upgrades_review_reject', { id: u.id }),
        c.title,
      ), reviewThumb(u.scene_id ? `/scene/${u.scene_id}/screenshot` : null, u.scene_id ? `/scenes/${u.scene_id}` : null)));
    });
    group.appendChild(ul);
    return group;
  }

  // buildSection is shared with the History page's "Imported" list.
  window.SRW_Activity = { render, buildSection, movieRows };
})();
