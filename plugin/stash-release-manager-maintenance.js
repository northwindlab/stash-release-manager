/**
 * StashReleaseManager - Maintenance page
 * Reachable at /plugins/stash-release-manager/maintenance.
 *
 * Anchors scripts/audit_library_fingerprints.py +
 * scripts/fix_library_mismatches.py's retroactive-mismatch workflow
 * permanently in the plugin itself (2026-09-16 request), instead of
 * only being runnable by hand from the terminal. A "Library Check"
 * (full library, or scoped to one or more directories picked via
 * Stash's own real directory browser -- see buildDirectoryPicker()
 * below) checks every local scene carrying a StashDB stash_id against
 * StashDB's own registered fingerprints (api/library_check.py,
 * api/routers/maintenance.py). Confirmed mismatches (exactly one
 * alternative candidate) can be selected individually or all at once
 * and corrected in bulk, Scene-Tagger-style; ambiguous/unverifiable
 * findings are shown for information only, same "never guess" rule
 * the CLI scripts follow.
 */
(function () {
  'use strict';

  let selectedIds = new Set();
  let pollTimer = null;

  // Maintenance areas (2026-10-04, UI review): every tool is its own page with its own address
  // (/plugins/<id>/maintenance/<key>), reached through the side navigation -- no more collapsed sections on one long page.
  const MAINTENANCE_PATH = '/plugins/stash-release-manager/maintenance';
  // Groups of the side menu (v0.153.0); the order here is the order in the menu. Keys and addresses are unchanged.
  const AREA_GROUPS = [
    { key: 'check', label: 'Check library' },
    { key: 'resolve', label: 'Resolve' },
    { key: 'files', label: 'Files & quality' },
    { key: 'movies', label: 'Movies' },
  ];
  const AREAS = [
    { key: 'library-check', group: 'check', icon: 'faClipboardCheck', label: 'Fingerprint check', build: (c) => renderLibraryCheck(c) },
    { key: 'performers', group: 'check', icon: 'faUserSlash', label: 'Performers without scenes', build: (c) => c.appendChild(buildPerformersArea()) },
    { key: 'unidentified', group: 'check', icon: 'faCircleQuestion', label: 'Unidentified scenes', build: (c) => c.appendChild(buildUnidentifiedArea()) },
    { key: 'vr-check', group: 'check', icon: 'faVrCardboard', label: 'VR: 2D / mono files', build: (c) => c.appendChild(flatten(buildVrCheckSection())) },
    { key: 'unknown-quality', group: 'check', icon: 'faQuestion', label: 'Unknown quality', build: (c) => { c.appendChild(window.SRW.el('p', { className: 'srw-hint', textContent: unknownQualityReason })); c.appendChild(buildUnknownQualitySection()); } },
    { key: 'review', group: 'resolve', icon: 'faFingerprint', label: 'Review: Identify Scene', build: (c) => buildReviewArea(c) },
    { key: 'compare', group: 'resolve', icon: 'faCodeCompare', label: 'Compare scenes', build: (c) => { c.appendChild(buildSenseNotice()); c.appendChild(flatten(buildCompareSection('compare'))); } },
    { key: 'import', group: 'files', icon: 'faFileImport', label: 'Import folder', build: (c) => { c.appendChild(window.SRW.buildSafetyPanel({ compact: true })); c.appendChild(buildImportSection()); } },
    { key: 'organize', group: 'files', icon: 'faFolderTree', label: 'Organize library', build: (c) => { c.appendChild(window.SRW.buildSafetyPanel({ compact: true })); c.appendChild(buildOrganizeSection()); } },
    { key: 'upgrades', group: 'files', icon: 'faCircleUp', label: 'Upgrade wanted', build: (c) => c.appendChild(flatten(buildUpgradeSection())) },
    { key: 'convert', group: 'files', icon: 'faRightLeft', label: 'Convert', build: (c) => c.appendChild(buildConvertSection()) },
    { key: 'movies', group: 'movies', icon: 'faFilm', label: 'Movies & groups', build: (c) => c.appendChild(buildMoviesSection()) },
    { key: 'duration-movies', group: 'movies', icon: 'faStopwatch', label: 'Duration: movie scenes', build: (c) => c.appendChild(buildDurationArea(true)) },
  ];

  // A tool that used to be a collapsible <details> panel is shown open and without the fold-away behaviour.
  function flatten(node) {
    if (node && node.tagName === 'DETAILS') {
      node.open = true;
      node.classList.add('srw-panel-flat');
    }
    return node;
  }

  let visibleAreas = AREAS;
  // "Unknown quality" (v0.106.2) only matters where Whisparr names/imports files itself (preset full_whisparr -- every other preset
  // imports with this sidecar's own naming, which no longer writes "Unknown"), or when such files are still in the library.
  let unknownQualityReason = '';
  // The badge of Organize library: the files of the checked studio list (v0.240.25). The list the page keeps in the browser is the newest (it is lowered
  // by every rename / move at once); the sidecar's number serves when this browser has none. No badge before the first Recount.
  function organizeBadge(fromSidecar) {
    try {
      const last = JSON.parse(window.localStorage.getItem('srw:organize-studios') || 'null');
      if (last && last.verified && Array.isArray(last.studios)) return last.studios.reduce((n, x) => n + (x.total || 0), 0);
    } catch (e) { /* no storage */ }
    return fromSidecar;
  }
  let watchSource = 'favorites'; // Settings > Watchlist source, read when the page is drawn: decides which button / icon a performer card shows

  // The area named in the address; without one the first area where something waits for a decision (Review, then Compare), else the first.
  let defaultAreaKey = null;
  function areaFromRoute() {
    const m = window.location.pathname.match(/\/maintenance\/([^/]+)/);
    const found = m && visibleAreas.find((a) => a.key === m[1]);
    return found ? found.key : (defaultAreaKey || visibleAreas[0].key);
  }

  // Face recognition is NOT built in (v0.172.0): the separate Stash-Sense 2 plugin does it on Stash's own scene page, and a call from here
  // could break with any update of that plugin. This plugin only points to it.
  const FACE_HINT = 'Face recognition: run Stash-Sense 2 on the scene page in Stash.';
  function buildFaceHint(s) {
    const { el } = window.SRW;
    return el('div', { className: 'srw-hint srw-face-hint' }, [
      `${FACE_HINT} `,
      el('a', { href: `/scenes/${s.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: `Open scene #${s.id} in Stash ↗` }),
    ]);
  }

  // Notice at the top of Review / Compare.
  function buildSenseNotice() {
    const { el } = window.SRW;
    return el('div', { className: 'srw-sense-notice' }, [
      el('strong', { textContent: 'Face recognition by Stash-Sense 2. ' }),
      'This plugin does not call it. To recognise the performers in a scene, open the scene in Stash (the link under each player) and run Stash-Sense 2 there '
        + '(it is a separate plugin with its own authors; it has to be installed and enabled in Stash).',
    ]);
  }

  let contentScope = 'both'; // Settings > Content, set by render() before any area is built
  function buildReviewArea(content) {
    content.appendChild(buildSenseNotice());
    content.appendChild(flatten(buildCompareSection('review')));
  }

  async function render(container) {
    const { el } = window.SRW;
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    container.innerHTML = '';
    container.appendChild(window.SRW.buildPageHeader('maintenance'));

    unknownQualityReason = '';
    let unknownQualityCount = 0;
    try {
      const settings = (await window.SRW.runOp('settings_get_all')) || {};
      watchSource = settings.watchlist_source === 'tag' ? 'tag' : 'favorites';
      if (settings.whisparr_scope_preset === 'full_whisparr') {
        unknownQualityReason = 'Shown because Whisparr imports files itself (Settings > Whisparr scope: full Whisparr management) -- its naming can write "Unknown".';
      } else {
        const found = (await window.SRW.runOp('unknown_quality_list')) || {};
        unknownQualityCount = (found.items || []).length;
        if ((found.items || []).length) unknownQualityReason = `Shown because ${found.items.length} file(s) with "Unknown" in the name are still in the library.`;
      }
    } catch (e) { /* hidden */ }
    // Settings > Content: the areas of the switched-off kind are hidden (Review, Import folder and Organize library serve both).
    const SCENE_AREAS = ['library-check', 'compare', 'unknown-quality', 'upgrades', 'vr-check'];
    const scope = await window.SRW.getContentScope();
    contentScope = scope;
    visibleAreas = AREAS.filter((a) => (a.key !== 'unknown-quality' || unknownQualityReason)
      && !(scope === 'movies' && SCENE_AREAS.includes(a.key)) && !(scope === 'scenes' && ['movies', 'duration-movies'].includes(a.key)));

    // What waits for a decision (one cheap local request): numbers behind Review, Compare and Upgrade wanted, and the area opened when the
    // address names none -- the first of Review, Compare that has something open.
    const countFor = (counts) => ({ review: counts && counts.review_open, compare: counts && counts.pairs_open, upgrades: counts && counts.upgrades_open, convert: counts && counts.convert_open,
      'library-check': counts && counts.library_check_open, 'vr-check': counts && counts.vr_open,
      'duration-movies': counts && counts.duration_movies_open, organize: organizeBadge(counts && counts.organize_open) });
    let badges = { ...countFor(null), 'unknown-quality': unknownQualityCount };
    try { badges = countFor(await window.SRW.runOp('maintenance_counts')); } catch (e) { /* no badges */ }
    defaultAreaKey = ['review', 'compare'].find((k) => badges[k] > 0 && visibleAreas.some((a) => a.key === k)) || null;

    const navItems = [];
    AREA_GROUPS.forEach((g) => {
      const inGroup = visibleAreas.filter((a) => a.group === g.key);
      if (!inGroup.length) return;
      navItems.push({ heading: g.label });
      inGroup.forEach((a) => navItems.push({ key: a.key, icon: a.icon, label: a.label, href: `${MAINTENANCE_PATH}/${a.key}`, badge: badges[a.key] }));
    });
    const content = el('div', { className: 'srw-maintenance-area' });
    let current = null;
    const side = window.SRW.buildSideNav(
      navItems,
      areaFromRoute(),
      (key) => {
        // Own address per area, drawn here without rebuilding the whole page (the watcher is told about the new address).
        try { history.pushState(null, '', `${MAINTENANCE_PATH}/${key}`); } catch (e) { /* ignore */ }
        window.SRW.acknowledgeUrl();
        showArea(key);
      },
    );
    container.appendChild(side.layout(content));

    function showArea(key) {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
      const area = visibleAreas.find((a) => a.key === key) || visibleAreas[0];
      current = area.key;
      side.setActive(area.key);
      content.innerHTML = '';
      area.build(content);
      refreshBadges();
    }
    // The numbers change while the areas are worked through: asked again whenever an area is opened.
    async function refreshBadges() {
      try {
        const fresh = countFor(await window.SRW.runOp('maintenance_counts'));
        ['review', 'compare', 'upgrades', 'convert', 'library-check', 'vr-check', 'duration-movies', 'organize'].forEach((k) => side.setBadge(k, fresh[k]));
      } catch (e) { /* keep the old numbers */ }
      refreshSlowBadges();
    }
    // The numbers that need Stash (performers, unidentified scenes, compare groups) come after the page is drawn; the sidecar keeps them for a minute.
    async function refreshSlowBadges() {
      try {
        const slow = (await window.SRW.runOp('maintenance_counts_slow')) || {};
        ['performers', 'unidentified', 'movies'].forEach((k) => { if (slow[k] != null) side.setBadge(k, slow[k]); });
      } catch (e) { /* no badge */ }
    }
    // Review (and others) tell the page when they changed what is open: the numbers behind the links follow at once.
    window.addEventListener('srw:counts-changed', refreshBadges);
    const start = areaFromRoute();
    // An address of an area that is not offered (e.g. a scene area while only movies are on) is corrected to the area shown.
    const asked = (window.location.pathname.match(/\/maintenance\/([^/]+)/) || [])[1];
    if (asked && asked !== start) {
      try { history.replaceState(null, '', `${MAINTENANCE_PATH}/${start}`); } catch (e) { /* ignore */ }
      window.SRW.acknowledgeUrl();
    }
    showArea(current || start);
  }

  async function renderLibraryCheck(content) {
    const { el } = window.SRW;
    let providers = [];
    let defaultProvider = 'stashdb';
    try {
      const pr = await window.SRW.runOp('maintenance_check_providers');
      providers = pr.providers || [];
      defaultProvider = pr.default || 'stashdb';
    } catch (e) { providers = [{ key: 'stashdb', name: 'StashDB', batch: true, submit: true }]; } // an older sidecar: StashDB as before
    fpProviders = Object.fromEntries(providers.map((p) => [p.key, p]));
    const providerSelect = el('select', { className: 'srw-filter-select', title: 'The metadata provider whose fingerprints your files are compared with' },
      providers.map((p) => el('option', { value: p.key, textContent: p.name })));
    providerSelect.value = defaultProvider;
    const chosen = () => fpProviders[providerSelect.value] || { key: 'stashdb', name: 'StashDB', batch: true };
    const heading = el('h3');
    const hint = el('p', { className: 'srw-hint' });
    const slowNote = el('p', { className: 'srw-hint', hidden: true });
    const fillTexts = () => {
      const n = chosen().name;
      // With several providers the drop-down names the one in use, the heading stays neutral (the findings below can come from either).
      heading.textContent = providers.length > 1 ? 'Fingerprint check: wrong scene IDs' : `Fingerprint check: wrong ${n} IDs`;
      hint.textContent = `Compares the fingerprints of your files with the ones ${n} has registered and finds scenes whose ${n} ID is wrong `
        + `-- a local scene pointing at the wrong ${n} scene -- and says what the file really is. A wrong ID can be corrected here (title, `
        + 'date, performers, studio and tags are taken over from the right scene); files and Whisparr are never touched. '
        + `Scenes ${n} cannot judge (no fingerprints registered yet, or several candidates) are listed under "Needs manual review".`;
      slowNote.hidden = chosen().batch !== false;
      slowNote.textContent = `${n} is asked one file at a time, so a check of the whole library takes long (about one request per second per file). `
        + 'Pick directories below to check a part, or re-check single scenes from "Needs manual review".';
    };
    fillTexts();
    providerSelect.addEventListener('change', fillTexts);
    content.appendChild(heading);
    if (providers.length > 1) content.appendChild(el('div', { className: 'srw-toolbar' }, [el('span', { textContent: 'Check against:' }), providerSelect]));
    content.appendChild(hint);
    content.appendChild(slowNote);
    if (!providers.length) {
      content.appendChild(el('p', { className: 'srw-release-mismatch-warning', textContent: 'No metadata provider is set up. Add StashDB, ThePornDB or another stash-box under Settings > Connections.' }));
    }

    const startBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Start fingerprint check', disabled: !providers.length });
    const progressWrap = el('div', { className: 'srw-maintenance-progress' });
    const resultsWrap = el('div', { className: 'srw-maintenance-results' });

    const picker = buildDirectoryPicker();
    content.appendChild(picker.el);
    content.appendChild(el('div', { className: 'srw-toolbar' }, [startBtn]));
    content.appendChild(progressWrap);
    content.appendChild(resultsWrap);

    startBtn.addEventListener('click', async () => {
      const scopedPaths = picker.getScopePaths();
      startBtn.disabled = true;
      resultsWrap.innerHTML = '';
      try {
        const started = await window.SRW.runOp('maintenance_check_run', { paths: scopedPaths, provider: providerSelect.value });
        if (started.skipped) {
          progressWrap.innerHTML = '';
          progressWrap.appendChild(el('span', { className: 'srw-hint', textContent: started.reason || 'A check is already running.' }));
          pollStatus(progressWrap, resultsWrap, startBtn);
          return;
        }
      } catch (e) {
        progressWrap.innerHTML = '';
        progressWrap.appendChild(el('span', { className: 'srw-release-mismatch-warning', textContent: `Failed to start: ${e.message}` }));
        startBtn.disabled = false;
        return;
      }
      pollStatus(progressWrap, resultsWrap, startBtn);
    });

    // Also reflect an already-running or already-finished check on
    // plain page load, not only right after clicking Start -- e.g.
    // reopening this page after navigating away mid-run.
    let status;
    try {
      status = await window.SRW.runOp('maintenance_check_status');
    } catch (e) {
      return;
    }
    if (status.phase === 'running') {
      startBtn.disabled = true;
      pollStatus(progressWrap, resultsWrap, startBtn);
    } else if (status.phase === 'done' || status.phase === 'failed') {
      renderProgress(progressWrap, status);
      const counts = await renderFindings(resultsWrap);
      renderProgress(progressWrap, status, counts);
    }
  }

  // Mirrors Stash's own native "Select folders" dialog (the one
  // behind Scan/Clean/Generate's "Selective..." buttons) as closely
  // as possible, using the exact same server-side directory browse
  // (stash_client.browse_directory(), confirmed live 2026-09-16 via
  // schema introspection to be the real `directory(path)` query Stash
  // itself uses) -- replaces an earlier flat "all 7 configured
  // top-level paths, checked by default" checkbox design that
  // couldn't pick a subdirectory at all.
  //
  // Interaction, same as Stash's own dialog: a "File path" box shows
  // the currently browsed directory; clicking a subdirectory row
  // navigates into it; "Up a directory" goes to its parent; "+ Add"
  // commits the CURRENTLY BROWSED path into a selected-paths list
  // (shown as removable chips) -- distinct from "the path you're
  // looking at right now". Quick-jump buttons for Stash's own
  // configured top-level library paths sit above the browser so
  // starting from e.g. /data/Scenes doesn't require manually
  // navigating there from the filesystem root every time.
  function buildDirectoryPicker() {
    const { el } = window.SRW;
    let mode = 'full'; // 'full' | 'selective'
    let selectedPaths = [];
    let libraryPaths = [];
    let browsePath = null;
    let browseParent = null;
    let browseDirs = [];

    const fullRadio = el('input', { type: 'radio', name: 'srw-maint-scope' });
    fullRadio.checked = true;
    const selectiveRadio = el('input', { type: 'radio', name: 'srw-maint-scope' });

    const shortcutsWrap = el('div', { className: 'srw-maintenance-shortcuts' });
    const chipsWrap = el('div', { className: 'srw-maintenance-chips' });
    const browserWrap = el('div', { className: 'srw-maintenance-browser' });
    const selectiveDetail = el('div', { className: 'srw-maintenance-selective', hidden: true }, [
      shortcutsWrap, chipsWrap, browserWrap,
    ]);

    const wrap = el('div', { className: 'srw-maintenance-paths' }, [
      el('div', { className: 'srw-hint' }, ['Library scope:']),
      el('label', { className: 'srw-maintenance-path-label' }, [fullRadio, ' Full library']),
      el('label', { className: 'srw-maintenance-path-label' }, [selectiveRadio, ' Select directories…']),
      selectiveDetail,
    ]);

    function setMode(next) {
      mode = next;
      selectiveDetail.hidden = mode !== 'selective';
    }
    fullRadio.addEventListener('change', () => { if (fullRadio.checked) setMode('full'); });
    selectiveRadio.addEventListener('change', () => { if (selectiveRadio.checked) setMode('selective'); });

    function renderChips() {
      chipsWrap.innerHTML = '';
      selectedPaths.forEach((p) => {
        const removeBtn = el('button', { className: 'srw-chip-remove', textContent: '×', title: 'Remove' });
        removeBtn.addEventListener('click', () => {
          selectedPaths = selectedPaths.filter((x) => x !== p);
          renderChips();
        });
        chipsWrap.appendChild(el('span', { className: 'srw-chip' }, [p, removeBtn]));
      });
    }

    async function browseTo(path) {
      browserWrap.innerHTML = '';
      browserWrap.appendChild(el('span', { className: 'srw-hint', textContent: 'Loading…' }));
      try {
        const res = await window.SRW.runOp('maintenance_directory_browse', path ? { path } : {});
        browsePath = res.path;
        browseParent = res.parent;
        browseDirs = res.directories || [];
      } catch (e) {
        browserWrap.innerHTML = '';
        browserWrap.appendChild(el('span', { className: 'srw-release-mismatch-warning', textContent: `Failed to browse: ${e.message}` }));
        return;
      }
      renderBrowser();
    }

    function renderBrowser() {
      browserWrap.innerHTML = '';
      const pathBox = el('input', { type: 'text' });
      pathBox.value = browsePath || '';
      pathBox.readOnly = true;
      const addBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: '+ Add' });
      addBtn.addEventListener('click', () => {
        if (browsePath && !selectedPaths.includes(browsePath)) {
          selectedPaths = [...selectedPaths, browsePath];
          renderChips();
        }
      });
      browserWrap.appendChild(el('div', { className: 'srw-maintenance-browse-row' }, [pathBox, addBtn]));

      const list = el('ul', { className: 'srw-maintenance-browse-list' });
      if (browseParent) {
        const upBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: '↑ Up a directory' });
        upBtn.addEventListener('click', () => browseTo(browseParent));
        list.appendChild(el('li', {}, [upBtn]));
      }
      browseDirs.forEach((d) => {
        const name = d.split('/').filter(Boolean).pop() || d;
        const dirBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: `📁 ${name}` });
        dirBtn.addEventListener('click', () => browseTo(d));
        list.appendChild(el('li', {}, [dirBtn]));
      });
      browserWrap.appendChild(list);
    }

    (async () => {
      try {
        const res = await window.SRW.runOp('maintenance_library_paths');
        libraryPaths = res.paths || [];
      } catch (e) {
        libraryPaths = [];
      }
      shortcutsWrap.appendChild(el('span', { className: 'srw-hint', textContent: 'Jump to: ' }));
      libraryPaths.forEach((p) => {
        const btn = el('button', { className: 'srw-btn srw-btn-sm', textContent: p });
        btn.addEventListener('click', () => browseTo(p));
        shortcutsWrap.appendChild(btn);
      });
      await browseTo(libraryPaths[0] || null);
    })();

    return {
      el: wrap,
      getScopePaths: () => (mode === 'full' ? [] : selectedPaths),
    };
  }

  // `pendingCounts` (optional {mismatchCount, reviewCount}) overrides
  // status.mismatch_count/unverifiable_count once known -- those two
  // status fields are just a tally of what THIS run itself found, but
  // the findings list below is the true CURRENT pending backlog across
  // every run ever (older findings from a differently-scoped run stay
  // pending until resolved, see library_check_repo.insert_finding()'s
  // per-scene dedup). Showing the run-scoped number here while the box
  // below shows the true total was confusing (user report 2026-09-16:
  // "unter der Progressbar steht 2 need review und in dem Kasten
  // Needs manual review steht 3") -- `match_count` alone stays
  // run-scoped, worded as such, since there's no comparable persisted
  // "matches" list anywhere else to disagree with it.
  function renderProgress(progressWrap, status, pendingCounts) {
    const { el } = window.SRW;
    progressWrap.innerHTML = '';
    if (!status.phase || status.phase === 'idle') return;
    const pct = status.total ? Math.round((status.current / status.total) * 100) : 0;
    let label;
    if (status.phase === 'running') {
      label = `Checking... ${status.current}/${status.total}`;
    } else if (status.phase === 'failed') {
      label = `Failed: ${status.error || 'unknown error'}`;
    } else {
      const mismatchCount = pendingCounts ? pendingCounts.mismatchCount : status.mismatch_count;
      const reviewCount = pendingCounts ? pendingCounts.reviewCount : status.unverifiable_count;
      label = `Done: ${status.match_count} match (this run). Pending: ${mismatchCount} mismatch, `
        + `${reviewCount} need review`;
    }
    progressWrap.appendChild(el('div', { className: 'srw-progress-track' }, [
      el('div', { className: 'srw-progress-fill', style: `width: ${pct}%` }),
    ]));
    progressWrap.appendChild(el('div', { className: 'srw-hint', textContent: label }));
  }

  function pollStatus(progressWrap, resultsWrap, startBtn) {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(async () => {
      let status;
      try {
        status = await window.SRW.runOp('maintenance_check_status');
      } catch (e) {
        return;
      }
      renderProgress(progressWrap, status);
      if (status.phase !== 'running') {
        clearInterval(pollTimer);
        pollTimer = null;
        startBtn.disabled = false;
        const counts = await renderFindings(resultsWrap);
        renderProgress(progressWrap, status, counts);
      }
    }, 2000);
  }

  // Returns {mismatchCount, reviewCount} -- the TRUE current pending
  // totals (see renderProgress()'s docstring above), so the caller can
  // reconcile the progress summary with what this function just
  // rendered.
  let fpProviders = {}; // Fingerprint check: provider key -> {key, name, batch, submit}, set by renderLibraryCheck()
  const fpName = (key) => (fpProviders[key || 'stashdb'] || {}).name || (key === 'theporndb' ? 'ThePornDB' : 'StashDB');
  let uploadEnabled = false; // Settings > fingerprint_upload_enabled, set by renderFindings(), read by the Needs-manual-review rows
  async function renderFindings(resultsWrap) {
    const { el } = window.SRW;
    resultsWrap.innerHTML = '';
    selectedIds = new Set();

    let mismatches = [];
    let others = [];
    try {
      const [mRes, aRes, uRes] = await Promise.all([
        window.SRW.runOp('maintenance_check_findings', { status: 'pending', kind: 'mismatch' }),
        window.SRW.runOp('maintenance_check_findings', { status: 'pending', kind: 'ambiguous' }),
        window.SRW.runOp('maintenance_check_findings', { status: 'pending', kind: 'unverifiable' }),
      ]);
      mismatches = mRes.findings || [];
      others = [...(aRes.findings || []), ...(uRes.findings || [])];
      // Settings > Schedule & Import: may the fingerprints of a confirmed file be submitted to StashDB?
      const appSettings = await window.SRW.runOp('settings_get_all').catch(() => ({}));
      uploadEnabled = !!appSettings && appSettings.fingerprint_upload_enabled === 'true';
    } catch (e) {
      resultsWrap.appendChild(el('span', { className: 'srw-release-mismatch-warning', textContent: `Failed to load findings: ${e.message}` }));
      return { mismatchCount: 0, reviewCount: 0 };
    }

    if (!mismatches.length && !others.length) {
      resultsWrap.appendChild(el('p', { className: 'srw-hint', textContent: 'No issues found.' }));
      return { mismatchCount: 0, reviewCount: 0 };
    }

    if (mismatches.length) {
      resultsWrap.appendChild(buildMismatchSection(mismatches, resultsWrap));
    }
    if (others.length) {
      resultsWrap.appendChild(buildInfoSection(others, resultsWrap));
    }
    return { mismatchCount: mismatches.length, reviewCount: others.length };
  }

  function buildMismatchSection(mismatches, resultsWrap) {
    const { el } = window.SRW;
    // "Select all" selects the findings of the CURRENT PAGE only (v0.157.0); when more match the search, a button selects all of them on purpose.
    const selectAll = el('input', { type: 'checkbox', title: 'Select every finding on this page' });
    const selectMore = el('button', { type: 'button', className: 'srw-btn' });
    selectMore.hidden = true;
    let pageFindings = [];
    let matchingFindings = [];
    const syncSelectAll = () => {
      selectAll.checked = pageFindings.length > 0 && pageFindings.every((f) => selectedIds.has(f.id));
      const rest = matchingFindings.filter((f) => !selectedIds.has(f.id)).length;
      selectMore.hidden = !(selectAll.checked && rest > 0);
      selectMore.textContent = `Select all ${matchingFindings.length} matching`;
    };
    const applyBtn = el('button', { className: 'srw-btn srw-btn-danger', textContent: 'Apply selected (0)' });
    applyBtn.disabled = true;

    const updateApplyBtn = () => {
      applyBtn.textContent = `Apply selected (${selectedIds.size})`;
      applyBtn.disabled = selectedIds.size === 0;
      syncSelectAll();
    };

    // Paged + searchable since 2026-09-26; the selection (selectedIds) lives across pages.
    let rowCheckboxes = [];
    const buildRow = (f) => {
      const cb = el('input', { type: 'checkbox' });
      cb.checked = selectedIds.has(f.id);
      rowCheckboxes.push(cb);
      cb.addEventListener('change', () => {
        if (cb.checked) selectedIds.add(f.id); else selectedIds.delete(f.id);
        updateApplyBtn();
      });
      const candidateTitle = (f.candidates[0] && f.candidates[0].title) || '?';
      const dismissBtn = el('button', { className: 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss', textContent: 'Dismiss' });
      dismissBtn.addEventListener('click', async () => {
        dismissBtn.disabled = true;
        try {
          const res = await window.SRW.runOp('maintenance_check_dismiss', { finding_id: f.id });
          if (res && res.error) throw new Error(res.error);
          window.SRW.dismissedNotice(`Dismissed: ${f.scene_title || 'finding'}`, '',
            () => window.SRW.runOp('maintenance_check_restore', { finding_id: f.id, status: res.previous || 'pending' }),
            () => renderFindings(resultsWrap));
          await renderFindings(resultsWrap);
        } catch (e) {
          window.SRW.notify('Dismiss', `Dismiss failed: ${e.message}`, 'error');
          dismissBtn.disabled = false;
        }
      });
      return el('li', { className: 'srw-release-item' }, [
        cb,
        el('span', { textContent: `${f.scene_title} → ${candidateTitle}` }),
        f.scene_id ? window.SRW.buildStashLink('scene', f.scene_id) : null,
        dismissBtn,
      ]);
    };
    const view = window.SRW.buildFilteredPagedList({
      filters: ['search'],
      perPage: 50,
      showFilterBarFrom: 10,
      fetchPage: async (f, page, perPage) => {
        const fetcher = window.SRW.localPageFetcher(() => mismatches, {
          text: (x) => `${x.scene_title} ${(x.candidates[0] && x.candidates[0].title) || ''}`,
        });
        const shown = await fetcher(f, page, perPage);
        pageFindings = shown.items || [];
        matchingFindings = (await fetcher(f, 1, 1000000)).items || []; // local data, no request
        syncSelectAll();
        return shown;
      },
      renderItems: (items) => {
        rowCheckboxes = [];
        return el('ul', { className: 'srw-release-list' }, items.map(buildRow));
      },
    });
    view.reset();

    selectAll.addEventListener('change', () => {
      pageFindings.forEach((f) => { if (selectAll.checked) selectedIds.add(f.id); else selectedIds.delete(f.id); });
      rowCheckboxes.forEach((cb) => { cb.checked = selectAll.checked; });
      updateApplyBtn();
    });
    selectMore.addEventListener('click', () => {
      matchingFindings.forEach((f) => selectedIds.add(f.id));
      updateApplyBtn();
      view.reload();
    });

    applyBtn.addEventListener('click', () => runApplySelected(mismatches, applyBtn, resultsWrap));

    return el('div', { className: 'srw-maintenance-mismatches' }, [
      el('h3', { textContent: `Mismatches (${mismatches.length})` }),
      el('div', { className: 'srw-toolbar' }, [
        el('label', {}, [selectAll, ' Select all on this page']),
        selectMore,
        applyBtn,
      ]),
      view.node,
    ]);
  }

  function runApplySelected(mismatches, applyBtn, resultsWrap) {
    const ids = Array.from(selectedIds);
    window.SRW.confirmInline(applyBtn,
      `Correct ${ids.length} scene(s)? Each one's title/date/details/studio/performers/tags and provider id `
      + `will be overwritten with the matched scene's own data. This only ever changes local Stash metadata `
      + `-- never a file on disk or Whisparr.`,
      async () => {
        // confirmInline hides applyBtn on Yes -- undone immediately
        // since it's also this loop's own progress indicator (its
        // textContent), same reasoning as entity-panel.js's
        // runBulkHandoff().
        applyBtn.style.display = '';
        applyBtn.disabled = true;
        let succeeded = 0;
        const failures = [];
        for (let i = 0; i < ids.length; i++) {
          const id = ids[i];
          const finding = mismatches.find((m) => m.id === id);
          const label = finding ? finding.scene_title : id;
          applyBtn.textContent = `Correcting... (${i + 1}/${ids.length})`;
          try {
            const result = await window.SRW.runOp('maintenance_check_apply', { finding_id: id });
            if (result.error) failures.push(`${label}: ${result.error}`);
            else succeeded += 1;
          } catch (e) {
            failures.push(`${label}: ${e.message}`);
          }
        }
        if (failures.length) {
          window.SRW.notify('Fingerprint check', `Corrected ${succeeded} of ${ids.length} scene(s). ${failures.length} failed:\n\n${failures.join('\n')}`, 'warning');
        }
        await renderFindings(resultsWrap);
      },
      { danger: true },
    );
  }

  // Ambiguous (2+ StashDB candidates) and unverifiable (no fingerprint
  // data at all) findings -- informational only, no correction is
  // safe to automate for either (see api/library_check.py's
  // find_library_mismatches() docstring). Only action offered is
  // Dismiss, to declutter the list once reviewed. Shows each file's
  // own local path(s) (2026-09-16 request) so they can be checked
  // directly on disk.
  function buildInfoSection(others, resultsWrap) {
    const { el } = window.SRW;
    const buildItem = (f) => {
      const detail = f.kind === 'ambiguous'
        ? `candidates: ${(f.candidates || []).map((c) => c.title).join(', ')}`
        : (f.reason || '');
      const dismissBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Dismiss' });
      dismissBtn.addEventListener('click', async () => {
        dismissBtn.disabled = true;
        try {
          const res = await window.SRW.runOp('maintenance_check_dismiss', { finding_id: f.id });
          if (res && res.error) throw new Error(res.error);
          window.SRW.dismissedNotice(`Dismissed: ${f.scene_title || 'finding'}`, '',
            () => window.SRW.runOp('maintenance_check_restore', { finding_id: f.id, status: res.previous || 'pending' }),
            () => renderFindings(resultsWrap));
          await renderFindings(resultsWrap);
        } catch (e) {
          window.SRW.notify('Dismiss', `Dismiss failed: ${e.message}`, 'error');
          dismissBtn.disabled = false;
        }
      });
      // "Re-check fingerprints": ask StashDB again what the file is -- fingerprints may have been registered since the check ran. A match closes
      // the finding, one other scene makes it a correction, the result is written under the row.
      const note = el('div', { className: 'srw-key-status srw-finding-note' });
      const recheckBtn = window.SRW.iconButton('button', 'faFingerprint', 'Re-check fingerprints', {
        title: `Ask ${fpName(f.provider)} again what this file is (fingerprints may have been registered since the check ran)`,
        onClick: async (e) => {
          const b = e.currentTarget;
          b.disabled = true;
          window.SRW.setBusy(note, `asking ${fpName(f.provider)}…`);
          try {
            const res = await window.SRW.runOp('maintenance_check_recheck', { finding_id: f.id });
            if (res.error) { note.textContent = ` error: ${res.error}`; return; }
            note.textContent = ` ${res.message}`;
            if (res.result !== 'unverifiable') {
              window.SRW.notify('Re-check fingerprints', `"${f.scene_title}": ${res.message}`, 'success');
              await renderFindings(resultsWrap);
            }
          } catch (err) {
            note.textContent = ` error: ${err.message}`;
          } finally {
            b.disabled = false;
          }
        },
      });
      // "File is correct" / "+ submit to StashDB" (v0.238.2, as on Activity > Import failed): only where StashDB simply has no fingerprint for the
      // file; the scene's id is right and the finding closes. The submit button is hidden while Settings > Schedule & Import > "Allow submitting
      // fingerprints" is off.
      const noMatch = f.kind === 'unverifiable' && /no fingerprint match/.test(f.reason || '');
      const confirmOp = (submit) => async () => {
        window.SRW.setBusy(note, submit ? 'submitting…' : 'applying…');
        const res = await window.SRW.runOp('maintenance_check_confirm', { finding_id: f.id, submit });
        if (res.error) { note.textContent = ` error: ${res.error}`; window.SRW.notify('File is correct', res.error, 'error'); return; }
        window.SRW.notify('File is correct', submit ? `"${f.scene_title}" is confirmed and its fingerprints were submitted to ${fpName(f.provider)}.`
          : `"${f.scene_title}" is confirmed.`, 'success');
        await renderFindings(resultsWrap);
      };
      const confirmBtn = noMatch ? window.SRW.iconButton('button', 'faCheck', 'File is correct', {
        title: `The scene's ${fpName(f.provider)} id is right (nobody registered a fingerprint for this file): close the finding`,
        onClick: (e) => window.SRW.confirmInline(e.currentTarget, `Mark "${f.scene_title}" as correct and close the finding?`, confirmOp(false)),
      }) : null;
      const canSubmit = (fpProviders[f.provider || 'stashdb'] || { submit: true }).submit;
      const submitBtn = noMatch && uploadEnabled && canSubmit ? window.SRW.iconButton('button', 'faCloudArrowUp', `Correct + submit to ${fpName(f.provider)}`, {
        title: `Like "File is correct", and the fingerprints of the file are submitted to ${fpName(f.provider)} (public, cannot be undone)`,
        onClick: (e) => window.SRW.confirmInline(e.currentTarget,
          `Mark "${f.scene_title}" as correct AND submit its fingerprints to ${fpName(f.provider)}? That is public and cannot be undone.`, confirmOp(true), { danger: true }),
      }) : null;
      const pathLines = (f.file_paths || []).map((p) => el('div', { className: 'srw-maintenance-file-path', textContent: p }));
      return el('li', { className: 'srw-release-item' }, [
        el('div', {}, [
          el('span', { textContent: `[${f.kind}] ${f.scene_title} -- ${detail}` }),
          ...pathLines,
          note,
        ]),
        f.scene_id ? window.SRW.buildStashLink('scene', f.scene_id) : null,
        recheckBtn,
        confirmBtn,
        submitBtn,
        dismissBtn,
      ]);
    };
    const view = window.SRW.buildFilteredPagedList({
      filters: ['search'],
      perPage: 50,
      showFilterBarFrom: 10,
      fetchPage: window.SRW.localPageFetcher(() => others, {
        text: (f) => `${f.kind} ${f.scene_title} ${(f.file_paths || []).join(' ')} ${f.reason || ''}`,
      }),
      renderItems: (items) => el('ul', { className: 'srw-release-list' }, items.map(buildItem)),
    });
    view.reset();

    return window.SRW.rememberOpen(el('details', { className: 'srw-panel' }, [
      el('summary', { textContent: `Needs manual review (${others.length})` }),
      view.node,
    ]), 'maintenance:needs-manual-review', false);
  }

  // ==================== VR check (2026-09-26, api/vr_check.py) ====================
  //
  // "VR scene, but the file is 2D": compares the left/right (and top/
  // bottom) halves of three frames per file -- both eyes of a real VR file
  // show nearly the same picture. Quick = only files with an unusual
  // aspect ratio; Full = every VR-tagged file (cached per file, so a
  // repeat only analyses new/changed files).
  let vrPollTimer = null;

  function buildVrCheckSection() {
    const { el, confirmInline } = window.SRW;
    const status = el('span', { className: 'srw-key-status' });
    const list = el('div');
    const dismissedList = el('div');
    let findings = []; // declared before the tabs: their labels read the counts straight away
    let dismissed = [];
    const counts = el('span', { hidden: true }); // text changes -> the tab labels follow (buildPaneTabs `watch`)
    const quickBtn = el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Quick check',
      title: 'Only VR files whose aspect ratio is neither ~2:1 nor ~1:1 (a few minutes)',
      onClick: () => start('quick'),
    });
    const fullBtn = el('button', {
      className: 'srw-btn', textContent: 'Full check',
      title: 'Every VR-tagged file (~1.7 s each, already checked files are skipped)',
      onClick: () => start('full'),
    });
    const section = el('details', { className: 'srw-settings-section srw-panel srw-vr-check' }, [
      el('summary', { textContent: 'VR scenes with a 2D or mono file' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Finds scenes tagged "Virtual Reality" whose file is actually flat (no left/right or top/bottom '
          + 'eye pair). Aspect ratio alone isn\'t enough -- "half SBS" VR files are 16:9 too -- so it compares the '
          + 'picture halves of three frames per file. For 2D/unclear files the local phash is also compared with '
          + 'the provider\'s fingerprints -- a match usually means an official 2D version of the scene. '
          + 'Monoscopic 360°/180° VR (single image, "VR mono") is listed as an upgrade candidate.',
      }),
      // What the pills lead to (v0.103.4): this page only LISTS findings; the search for a better file is a separate area.
      el('p', { className: 'srw-hint' }, [
        el('strong', { textContent: 'What happens with a finding: ' }),
        'This list holds the files the check could not judge (“unclear”) and the ones it judged flat although the scene is tagged VR: it is a suspicion until you say what the file is (Frame from file). ',
        '“2D” and “VR mono” make the file an upgrade candidate in ',
        el('a', { href: '/plugins/stash-release-manager/maintenance/upgrades', textContent: 'Upgrade wanted' }),
        ' (searched there); “Stereo (3D)” says it is fine as it is. “Dismiss” also means fine, without a verdict (it stays under “Dismissed”, with Restore). A file the check judged “VR mono” by its name goes to Upgrade wanted without waiting here.',
      ]),
      el('div', { className: 'srw-toolbar' }, [quickBtn, fullBtn, status]),
      window.SRW.buildPaneTabs([
        { key: 'findings', label: () => `Findings (${findings.length})`, pane: list, watch: counts },
        { key: 'dismissed', label: () => `Dismissed (${dismissed.length})`, pane: dismissedList, watch: counts },
      ], 'srw:vr-section'),
      list,
      dismissedList,
      counts,
    ]);

    async function start(mode) {
      quickBtn.disabled = true; fullBtn.disabled = true;
      const result = await window.SRW.runOp('vr_check_run', { mode });
      if (result.error) { status.textContent = ` error: ${result.error}`; }
      poll();
    }

    async function poll() {
      clearTimeout(vrPollTimer);
      const st = await window.SRW.runOp('vr_check_status');
      const job = (st && st.job) || {};
      quickBtn.disabled = !!job.running; fullBtn.disabled = !!job.running;
      if (job.running) {
        status.textContent = job.phase === 'checking'
          ? ` ${job.mode} check: ${job.current} / ${job.total}${job.title ? ` — ${job.title}` : ''}`
          : ' preparing…';
        vrPollTimer = setTimeout(poll, 3000);
      } else {
        const last = st && st.last_run;
        status.textContent = last
          ? ` last ${last.mode} check ${String(last.finished_at || '').replace('T', ' ').slice(0, 16)} UTC: `
            + `${last.files} file(s), ${last.analysed} newly analysed -- ${last.stereo} stereo, ${last.mono} 2D, `
            + `${last.vr_mono || 0} VR mono, ${last.unclear} unclear, ${last.error} error(s)`
          : ' not run yet';
      }
      await loadFindings();
    }

    let view = null;
    let dismissedView = null;
    async function loadFindings() {
      const [res, resDismissed] = await Promise.all([
        window.SRW.runOp('vr_check_findings', { status: 'pending' }),
        window.SRW.runOp('vr_check_findings', { status: 'dismissed' }),
      ]);
      findings = (res && res.findings) || [];
      dismissed = (resDismissed && resDismissed.findings) || [];
      counts.textContent = `${findings.length}/${dismissed.length}`;
      if (!dismissedView) {
        dismissedView = window.SRW.buildFilteredPagedList({
          filters: ['search'],
          perPage: 40,
          showFilterBarFrom: 10,
          emptyText: 'Nothing dismissed.',
          fetchPage: window.SRW.localPageFetcher(() => dismissed, {
            text: (f) => `${f.scene_title} ${f.file_path}`,
          }),
          renderItems: (items) => el('ul', { className: 'srw-release-list' }, items.map((f) => buildRow(f, true))),
        });
        dismissedList.appendChild(dismissedView.node);
      }
      dismissedView.reload();
      if (!view) {
        view = window.SRW.buildFilteredPagedList({
          filters: ['search'],
          perPage: 40,
          showFilterBarFrom: 10,
          emptyText: 'No 2D files found (among the files checked so far).',
          fetchPage: window.SRW.localPageFetcher(() => findings, {
            text: (f) => `${f.scene_title} ${f.file_path}`,
          }),
          renderItems: (items) => el('ul', { className: 'srw-release-list' }, items.map((f) => buildRow(f))),
        });
        list.appendChild(view.node);
      }
      view.reload();
    }

    function buildRow(f, isDismissed = false) {
      const verdict = f.verdict === 'mono' ? '2D' : (f.verdict === 'vr_mono' ? 'VR mono' : 'unclear');
      // phash (checked live 2026-09-26): most flat files DO match a provider
      // fingerprint -- the providers also list the studios' official 2D
      // versions -- so a match means "known 2D version of this scene",
      // not "correct VR file"; NO match hints at an unknown re-encode.
      const phash = f.phash_match === 1 ? 'phash matches a provider fingerprint (likely an official 2D version)'
        : (f.phash_match === 0 ? 'phash matches NO provider fingerprint (unknown encode)' : 'phash not compared');
      const scores = `L/R ${f.lr_score != null ? f.lr_score.toFixed(2) : '?'}, T/B ${f.tb_score != null ? f.tb_score.toFixed(2) : '?'}`;
      const remove = () => {
        const at = findings.findIndex((x) => x.id === f.id);
        findings = findings.filter((x) => x.id !== f.id);
        view.reload();
        return at;
      };
      const dismissBtn = el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Dismiss', title: 'The file is fine -- don\'t show it again (a 2D file then gets no upgrade search)',
        onClick: async () => {
          const r = await window.SRW.runOp('vr_check_dismiss', { finding_id: f.id });
          if (r.error) { window.SRW.notify('VR check', r.error, 'error'); return; }
          window.SRW.dismissedNotice(`Dismissed: ${f.scene_title || 'VR finding'}`, 'Find it again under “Dismissed”.',
            () => window.SRW.runOp('vr_check_restore', { finding_id: f.id, status: r.previous || 'pending' }),
            () => loadFindings());
          await loadFindings();
        },
      });
      // Stash's screenshot is often the scene's cover, not a frame of this
      // file -- this grabs a real frame from the middle of the file.
      const frameBtn = el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Frame from file',
        title: 'Show a frame from the middle of this file (two eyes side by side = VR)',
        onClick: async () => {
          frameBtn.disabled = true;
          frameBtn.textContent = 'Loading…';
          const r = await window.SRW.runOp('vr_check_frame', { finding_id: f.id });
          frameBtn.disabled = false;
          frameBtn.textContent = 'Frame from file';
          if (r.error) { window.SRW.notify('Frame from file', r.error, 'error'); return; }
          thumb.querySelector('img').src = r.data_url;
          thumb.querySelector('img').classList.add('srw-vr-thumb-frame');
          thumb.title = 'Frame from the file -- click to open the scene in Stash';
        },
      });
      // Own verdict (v0.105.0): the check can be wrong (a 360° mono file judged "2D"). Kept over re-checks of the same file.
      const verdictBtns = [['mono', '2D'], ['vr_mono', 'VR mono'], ['stereo', 'Stereo (3D)']].map(([value, label]) => el('button', {
        // The check's own judgement ("2D") is only a suspicion: the same button confirms it (v0.239.0). Disabled only once it is YOUR verdict.
        className: `srw-btn srw-btn-sm${f.verdict === value && f.verdict_manual ? ' srw-btn-primary' : ''}`, textContent: label,
        title: f.verdict === value && f.verdict_manual ? `Currently: ${label} (set by you)`
          : (f.verdict === value ? `The check judged this file ${label}: click to confirm` : `This file is: ${label}`),
        ...(f.verdict === value && f.verdict_manual ? { disabled: true } : {}),
        onClick: async () => {
          const r = await window.SRW.runOp('vr_check_set_verdict', { finding_id: f.id, verdict: value });
          if (r.error) { window.SRW.notify('VR check', r.error, 'error'); return; }
          const prev = r.previous || {};
          // The finding is decided: it leaves this list (loadFindings below), 2D / VR mono go on to Upgrade wanted.
          await loadFindings();
          window.SRW.dismissedNotice(`Marked as ${label}: ${f.scene_title || 'file'}`,
            value === 'stereo' ? 'Kept when the check runs again. A stereo file is fine; it is only an upgrade candidate when its bitrate is low.'
              : 'Kept when the check runs again. The file shows up in Upgrade wanted at its next refresh.',
            () => window.SRW.runOp('vr_check_restore_verdict', { finding_id: f.id, verdict: prev.verdict, manual: !!prev.verdict_manual,
              finding_status: prev.finding_status || 'pending' }),
            () => loadFindings());
        },
      }));
      const identifyBtn = el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Identify Scene',
        title: 'The file is not the scene it is filed under: send this scene to Review > Identify Scene',
        onClick: async () => {
          const r = await window.SRW.runOp('compare_review_add', { scene_id: f.scene_id });
          if (r.error) { window.SRW.notify('Identify Scene', r.error, 'error'); return; }
          if (r.already) { window.SRW.notify('Identify Scene', 'The scene is already in the Review list.', 'info'); return; }
          window.SRW.dismissedNotice(`Sent to Identify Scene: ${f.scene_title || 'scene'}`, 'Tag “Review: Identify Scene” added (nothing else changed).',
            () => window.SRW.runOp('compare_review_done', { scene_id: f.scene_id }), null);
        },
      });
      const restoreBtn = window.SRW.iconButton('button', 'faRotateLeft', 'Restore', {
        className: 'srw-btn srw-btn-sm srw-btn-primary',
        title: 'Back to the findings (the file is judged as a finding again)',
        onClick: async () => {
          const r = await window.SRW.runOp('vr_check_restore', { finding_id: f.id, status: 'pending' });
          if (r.error) { window.SRW.notify('Restore', r.error, 'error'); return; }
          window.SRW.notify('Restore', `“${f.scene_title || 'file'}” is back under Findings.`, 'success');
          await loadFindings();
        },
      });
      const actions = isDismissed ? [restoreBtn, identifyBtn, frameBtn] : [...verdictBtns, identifyBtn, frameBtn, dismissBtn];
      if (!isDismissed && f.sidecar_release) {
        actions.unshift(el('button', {
          className: 'srw-btn srw-btn-sm srw-btn-danger', textContent: 'Fix wrong download',
          onClick: (e) => confirmInline(e.currentTarget,
            `Blocklist this download of "${f.scene_title}", move the file to the recycle bin and search again?`,
            async () => {
              const r = await window.SRW.runOp('vr_check_fix', { finding_id: f.id });
              if (r.error) { window.SRW.notify('Fix wrong download', r.error, 'error'); return; }
              window.SRW.notify('Fix wrong download', `"${f.scene_title}": blocklisted${r.blocklisted_title ? ` (${r.blocklisted_title})` : ''}, file moved to the recycle bin.`, 'success');
              remove();
            }, { danger: true }),
        }));
      }
      // Quick visual check (2026-09-26, user request): Stash's own scene
      // screenshot as thumbnail -- a real VR file shows two eyes side by
      // side (or stacked) right there -- and a clearly styled title link;
      // both open the scene in a new tab so this list stays put.
      const sceneHref = `/scenes/${f.scene_id}`;
      const thumb = el('a', {
        className: 'srw-vr-thumb-link', href: sceneHref, target: '_blank', rel: 'noopener noreferrer',
        title: 'Open the scene in Stash (new tab)',
      }, [el('img', { className: 'srw-vr-thumb', src: `/scene/${f.scene_id}/screenshot`, loading: 'lazy', alt: '' })]);
      return el('li', { className: 'srw-release-item' }, [
        el('span', { className: `srw-vr-verdict srw-vr-verdict-${f.verdict}`, textContent: verdict }),
        thumb,
        el('div', { className: 'srw-row-info' }, [
          el('a', {
            className: 'srw-release-title', href: sceneHref, target: '_blank', rel: 'noopener noreferrer',
            textContent: f.scene_title || `Scene ${f.scene_id}`,
          }),
          el('div', { className: 'srw-hint', textContent: `${f.width}×${f.height} — ${scores}`
            + `${f.verdict === 'vr_mono' ? '' : ` — ${phash}`}` }),
          // Monoscopic 360/180 VR (2026-09-26, user decision): fine to keep,
          // but should be replaced by a stereoscopic version as an upgrade.
          f.verdict === 'vr_mono' ? el('div', {
            className: 'srw-hint srw-vr-mono-note',
            textContent: 'Monoscopic 360°/180° VR -- OK to keep, upgrade candidate: replace with a stereoscopic '
              + 'version when one is available.',
          }) : null,
          // No phash match at all (2026-09-26, user-confirmed on candlj.mpg):
          // the file may not even be the scene Stash links it to.
          f.phash_match === 0 ? el('div', {
            className: 'srw-hint srw-vr-misid',
            textContent: 'Possibly misidentified in Stash (file matches none of the scene\'s fingerprints) -- '
              + 'use “Identify Scene” (or the Fingerprint check above) to find the right scene.',
          }) : null,
          el('div', { className: 'srw-maintenance-file-path', textContent: f.file_path }),
        ]),
        el('div', { className: 'srw-row-actions' }, actions),
      ]);
    }

    poll();
    return window.SRW.rememberOpen(section, 'maintenance:vr-check', false);
  }

  // ==================== Upgrade search (2026-09-26, api/upgrades.py) ====================
  const UPGRADE_REASON = {
    // Badge = current file → upgrade target (2026-09-26: "VR → real VR" read
    // as if the file were already VR).
    vr_2d: ['2D → VR', 'VR scene, but the file is 2D -- replace with real stereoscopic VR'],
    vr_mono: ['mono → stereo', 'monoscopic 360°/180° VR, should become stereoscopic'],
    vr_low_quality: ['VR quality', 'VR below 4K or below the minimum bitrate'],
    below_cutoff: ['below cutoff', 'below the quality profile\'s cutoff resolution'],
    readd: ['re-added', 'you asked for this scene again (Search page: Re-add)'],
    duration: ['wrong length', 'the file is clearly shorter or longer than the provider\'s length (a cut or partial rip, a sample, or a wrong length at the provider)'],
  };

  // Duration mismatch (v0.209.0, api/duration_check.py): library scenes whose file is clearly shorter / longer than the provider says.
  function buildDurationArea(inGroup = false) {
    const { el, confirmInline, notify } = window.SRW;
    let findings = [];
    let direction = '';
    let threshold = 5;
    let showIgnored = false;
    const status = el('span', { className: 'srw-key-status' });
    const summary = el('div', { className: 'srw-hint' });
    // [h:]mm:ss
    const fmt = (sec) => {
      const t = Math.round(sec || 0);
      const h = Math.floor(t / 3600);
      const mm = String(Math.floor((t % 3600) / 60)).padStart(2, '0');
      const ss = String(t % 60).padStart(2, '0');
      return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
    };
    const view = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 30, showFilterBarFrom: 10, emptyText: inGroup ? 'No scene of a movie deviates from the provider\'s length by more than the threshold.' : 'No scene deviates from the provider\'s length by more than the threshold.',
      fetchPage: window.SRW.localPageFetcher(() => findings, { text: (x) => `${x.scene_title} ${x.file_path}` }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => {
        const rowStatus = el('span', { className: 'srw-key-status' });
        const pct = Math.round(Math.abs(x.deviation) * 100);
        const shorter = x.deviation < 0;
        const providerUrl = window.SRW.sceneUrl(x.provider, x.provider_scene_id);
        const sceneHref = `/scenes/${x.scene_id}`;
        const busy = x.upgrade_status ? ` — ${x.upgrade_status === 'wanted' ? 'queued for a better file' : x.upgrade_status}` : '';
        const queued = x.upgrade_status === 'wanted' || x.upgrade_status === 'grabbed';
        const run = async (search) => {
          window.SRW.setBusy(rowStatus, search ? 're-adding and searching...' : 're-adding...');
          const res = await window.SRW.runOp('duration_check_readd', { scene_id: x.scene_id, search });
          if (res.error) { rowStatus.textContent = ` error: ${res.error}`; return; }
          window.SRW.notifyReadd(x.scene_title, res);
          rowStatus.textContent = '';
          x.upgrade_status = res.grabbed ? 'grabbed' : 'wanted';
          view.reload();
        };
        // "Dismiss" / "Restore" like everywhere else (v0.239.2; it was "Ignore" / "Show again"), with the usual Undo.
        const wasDismissed = x.status === 'ignored';
        const ignoreBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: wasDismissed ? 'Restore' : 'Dismiss',
          title: wasDismissed ? 'Put it back on the list' : 'The length is right (or does not matter): take it off the list',
          onClick: async () => {
            const res = await window.SRW.runOp('duration_check_ignore', { scene_id: x.scene_id, restore: wasDismissed });
            if (res.error) { rowStatus.textContent = ` error: ${res.error}`; return; }
            findings = findings.filter((f) => f.scene_id !== x.scene_id); view.reload(); updateSummary();
            if (!wasDismissed) {
              window.SRW.dismissedNotice(`Dismissed: ${x.scene_title || 'scene'}`, 'Find it again with “show dismissed”.',
                () => window.SRW.runOp('duration_check_ignore', { scene_id: x.scene_id, restore: true }), () => load());
            }
          } });
        const actions = [
          window.SRW.iconButton('a', 'faArrowUpRightFromSquare', 'Provider page', { className: 'srw-btn srw-btn-sm', href: providerUrl, target: '_blank', rel: 'noopener noreferrer',
            title: 'Open the scene on the provider\'s website (new tab)' }),
          queued || inGroup ? null : window.SRW.iconButton('button', 'faRotateRight', 'Re-add', { className: 'srw-btn srw-btn-sm',
            title: 'Put it on the upgrade list: the scheduled search looks for a release of the right length; a verified download replaces this file (the old one goes to the recycle bin)',
            onClick: (e) => confirmInline(e.currentTarget, `Re-add “${x.scene_title}”? A release of the right length replaces this file after a verified download; the old file goes to the recycle bin. It is searched by the schedule, not right now.`, () => run(false)) }),
          queued || inGroup ? null : window.SRW.iconButton('button', ['faRotateRight', 'faMagnifyingGlass'], 'Re-add + search', { className: 'srw-btn srw-btn-sm srw-btn-primary',
            title: 'The same, and ask the indexers right now',
            onClick: (e) => confirmInline(e.currentTarget, `Re-add “${x.scene_title}” and search now? A release of the right length replaces this file after a verified download; the old file goes to the recycle bin.`, () => run(true)) }),
          ignoreBtn,
        ].filter(Boolean);
        return el('li', { className: 'srw-release-item' }, [
          el('span', { className: `srw-upgrade-reason srw-upgrade-reason-${shorter ? 'dur_short' : 'dur_long'}`, textContent: shorter ? 'file shorter' : 'file longer',
            title: shorter ? 'The file is shorter than the provider\'s length: a sample or a cut / broken download' : 'The file is longer than the provider\'s length' }),
          el('a', { className: 'srw-vr-thumb-link', href: sceneHref, target: '_blank', rel: 'noopener noreferrer' },
            [el('img', { className: 'srw-vr-thumb', src: `/scene/${x.scene_id}/screenshot`, loading: 'lazy', alt: '' })]),
          el('div', {}, [
            el('a', { className: 'srw-release-title', href: sceneHref, target: '_blank', rel: 'noopener noreferrer',
              textContent: x.scene_title || x.file_path || `Scene ${x.scene_id}` }),
            el('div', { className: 'srw-hint', textContent: `file ${fmt(x.file_sec)} · ${window.SRW.providerLabel(x.provider)} ${fmt(x.provider_sec)} — `
              + `${shorter ? 'shorter' : 'longer'} by ${fmt(Math.abs(x.file_sec - x.provider_sec))} (${pct}%)${busy}` }),
            el('div', { className: 'srw-maintenance-file-path', textContent: x.file_path }),
            rowStatus,
          ]),
          el('div', { className: 'srw-upgrade-actions' }, actions),
        ]);
      })),
    });
    const dirSel = el('select', { onChange: (e) => { direction = e.target.value; load(); } },
      [['', 'file shorter or longer'], ['shorter', 'file shorter (samples, cut files)'], ['longer', 'file longer']].map(([v, t]) => el('option', { value: v, textContent: t })));
    const thrInput = el('input', { type: 'number', min: '1', max: '90', value: String(threshold), className: 'srw-input-narrow',
      title: 'Deviation in percent', onChange: (e) => { threshold = Math.max(1, Math.min(90, Number(e.target.value) || 5)); load(); } });
    const ignoredBox = el('input', { type: 'checkbox', onChange: (e) => { showIgnored = e.target.checked; load(); } });
    const runBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Run check',
      title: 'Compares the length of every library file with the provider\'s length (StashDB / ThePornDB). The first run asks the providers about every scene and takes a while; later runs only ask about new ones.' });
    function updateSummary(job, total) {
      const j = job || {};
      const running = j.running ? ` — running: ${j.phase || ''} ${j.done || 0}/${j.total || 0}${j.errors ? `, ${j.errors} lookup error(s)` : ''}` : '';
      summary.textContent = `${findings.length} scene(s) over ${threshold} %${total ? ` — ${total.with_provider_length} of ${total.checked} checked scenes have a provider length` : ''}`
        + (total && total.last_checked ? ` (last answer ${total.last_checked} UTC)` : '') + running;
    }
    async function load() {
      const [f, st] = await Promise.all([
        window.SRW.runOp('duration_check_findings', { threshold, direction, ignored: showIgnored, groups: inGroup ? 'only' : 'exclude' }),
        window.SRW.runOp('duration_check_status'),
      ]);
      if (f.error) { status.textContent = ` error: ${f.error}`; return; }
      findings = f.findings || [];
      view.reset(); updateSummary(st.job, st.error ? null : st);
      return st.job && st.job.running;
    }
    let timer = null;
    async function poll() {
      clearTimeout(timer);
      const running = await load();
      runBtn.disabled = Boolean(running);
      if (running) timer = setTimeout(poll, 4000);
    }
    runBtn.addEventListener('click', async () => {
      const r = await window.SRW.runOp('duration_check_run', {});
      status.textContent = r.error ? ` error: ${r.error}` : (r.skipped ? ` ${r.reason}` : ' started');
      poll();
    });
    poll();
    return el('div', { className: 'srw-settings-section' }, [
      el('p', { className: 'srw-hint', textContent: inGroup
        ? 'Scenes that belong to a movie (a group) whose file is clearly shorter or longer than the length the provider has. They are kept apart from the library list because a movie’s scenes are cut and numbered differently from a normal scene; there is no “Re-add” here — look at the file and the movie yourself. “Run check” is the same run as the duration check in Upgrade wanted (reason: wrong length).'
        : 'Library scenes whose file is clearly shorter or longer than the length the provider has — samples, cut or broken downloads. Scenes that are part of a movie are checked under Movies > Duration: movie scenes. Scenes the provider has no length for are not checked. “Re-add” puts the scene on the upgrade list: a release of about the right length replaces the file (the old one goes to the recycle bin).' }),
      el('div', { className: 'srw-toolbar' }, [runBtn, dirSel, el('label', {}, ['deviation over ', thrInput, ' %']), el('label', {}, [ignoredBox, ' show dismissed']), status]),
      summary, view.node,
    ]);
  }

  function buildOrganizeSection() {
    const { el, confirmInline } = window.SRW;
    let studios = [];
    const status = el('span', { className: 'srw-key-status' });
    // The plan of one studio opens in a modal (v0.240.10); planSlot is its content.
    const planSlot = el('div');
    let planModal = null;
    const short = (p) => p.replace(/^.*?\/scenes\//, '');
    const studioView = window.SRW.buildFilteredPagedList({
      filters: ['search'],
      perPage: 25,
      emptyText: 'Nothing to organize.',
      fetchPage: window.SRW.localPageFetcher(() => studios, { text: (x) => x.studio }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => el('li', { className: 'srw-release-item' }, [
        el('strong', { textContent: x.studio }),
        el('span', { className: 'srw-release-date', textContent: `${x.rename} rename · ${x.move} move${x.other ? ` · ${x.other} to check` : ''}` }),
        window.SRW.iconButton('button', 'faList', 'Details…', { className: 'srw-btn srw-btn-sm', title: 'Details: show which files would be renamed / moved, and rename or move them', onClick: () => showPlan(x) }),
      ]))),
    });

    // "Load studios" reads the sidecar's list (the rough local count until the first Recount); "Recount" runs in the background (v0.240.17): every studio
    // with local candidates gets its real plan -- provider data and fingerprints -- and only studios with something to do stay in the list.
    let studiosVerified = false;
    const recountBtn = el('button', { className: 'srw-btn', textContent: 'Recount',
      title: 'Check every studio against StashDB (takes a few minutes, runs in the background); studios with nothing to do leave the list', onClick: () => recount() });
    async function loadStudios() {
      status.textContent = ' loading…';
      const r = await window.SRW.runOp('organize_studios', { refresh: false });
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      studios = r.studios || [];
      studiosVerified = Boolean(r.verified);
      const at = Date.now();
      try { window.localStorage.setItem(STUDIOS_KEY, JSON.stringify({ studios, at, verified: studiosVerified })); } catch (e) { /* too big or blocked: just not remembered */ }
      showStudios(at);
    }
    async function pollRecount() {
      recountBtn.disabled = true;
      for (;;) {
        const st = await window.SRW.runOp('organize_recount_status');
        if (st.error) { status.textContent = ` error: ${st.error}`; break; }
        if (!st.running) {
          if (st.error) status.textContent = ` Recount failed: ${st.error}`;
          else await loadStudios();
          break;
        }
        status.textContent = st.studio ? ` checking ${st.studio} (${st.done || 0} / ${st.total || '?'})…`
          : ` ${st.phase || 'starting'}…${st.total ? ` (${st.total} studios to check)` : ''}`;
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
      recountBtn.disabled = false;
    }
    async function recount() {
      const r = await window.SRW.runOp('organize_recount_start');
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      pollRecount();
    }

    // The list is kept in the browser between visits (v0.240.9), like the analysis on Import folder.
    const STUDIOS_KEY = 'srw:organize-studios';
    function showStudios(at) {
      const files = studios.reduce((n, x) => n + x.total, 0);
      status.textContent = ` ${studios.length} studio(s), ${files} file(s) to organize (counted ${new Date(at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })})`
        + (studiosVerified ? '' : ' — a rough count from the local titles; "Recount" checks every studio against StashDB');
      studioView.reset();
      window.dispatchEvent(new Event('srw:counts-changed')); // the badge of the side menu follows
    }

    function itemRow(x, kind, studio) {
      const rowStatus = el('span', { className: 'srw-key-status' });
      const parts = [
        el('a', {
          className: 'srw-release-title', href: `/scenes/${x.scene_id}`, target: '_blank',
          rel: 'noopener noreferrer', textContent: x.title,
        }),
        x.duplicate ? el('span', { className: 'srw-history-category', textContent: 'identical copy elsewhere' }) : null,
        el('div', { className: 'srw-review-box' }, [
          el('span', { className: 'srw-history-message', textContent: `${short(x.path)}\n→ ${short(x.new_path)}` }),
        ]),
        window.SRW.buildStashLink('scene', x.scene_id),
      ];
      if (kind === 'rename' || kind === 'move') {
        parts.push(el('button', {
          className: 'srw-btn srw-btn-sm', textContent: kind === 'rename' ? 'Rename' : 'Move',
          onClick: async (e) => {
            e.target.disabled = true;
            rowStatus.textContent = ' working…';
            const r = await window.SRW.runOp('organize_apply', { studio_id: studio.studio_id, file_ids: [x.file_id], kinds: [kind] });
            const failed = (r.failed || [])[0];
            rowStatus.textContent = r.error ? ` error: ${r.error}` : failed ? ` failed: ${failed.reason}` : ' done';
            if (!r.error && !failed) showPlan(studio, true);
            else e.target.disabled = false;
          },
        }), rowStatus);
      } else {
        parts.push(el('span', { className: 'srw-release-date', textContent: x.reason || x.check }), rowStatus);
        if (kind === 'check') {
          // A file the fingerprints could not confirm (v0.240.15): find out what it is, send it to Identify Scene, or say it is the right one.
          const sameFolder = x.path.slice(0, x.path.lastIndexOf('/')) === x.new_path.slice(0, x.new_path.lastIndexOf('/'));
          const verb = sameFolder ? 'Rename' : 'Move';
          parts.push(window.SRW.iconButton('button', 'faFingerprint', 'Check what the file is', {
            className: 'srw-btn srw-btn-sm', title: 'Ask the metadata provider what this file is, by its fingerprints (nothing is changed)',
            onClick: async (e) => {
              const b = e.currentTarget;
              b.disabled = true; window.SRW.setBusy(rowStatus, 'asking the provider…');
              try {
                const r = await window.SRW.runOp('organize_check_file', { scene_id: x.scene_id });
                rowStatus.textContent = r.error ? ` error: ${r.error}` : ` ${r.message}`;
              } finally { b.disabled = false; }
            },
          }));
          parts.push(window.SRW.iconButton('button', ['faFingerprint', 'faMagnifyingGlass'], 'Send to Identify Scene', {
            className: 'srw-btn srw-btn-sm', title: 'The file is not the scene it is tagged with: send this scene to Review > Identify Scene',
            onClick: async (e) => {
              const b = e.currentTarget;
              b.disabled = true;
              const r = await window.SRW.runOp('compare_review_add', { scene_id: x.scene_id });
              if (r.error) { rowStatus.textContent = ` error: ${r.error}`; b.disabled = false; return; }
              rowStatus.textContent = r.already ? ' is in Review: Identify Scene already' : ' sent to Review: Identify Scene';
            },
          }));
          parts.push(el('button', {
            className: 'srw-btn srw-btn-sm', textContent: `${verb} anyway`,
            title: `The file is the right one although the fingerprints could not confirm it: ${verb.toLowerCase()} it to the name shown`,
            onClick: (e) => confirmInline(e.currentTarget, `${verb} "${x.title}" although its fingerprints could not confirm it? Stash keeps the scene.`, async () => {
              rowStatus.textContent = ' working…';
              const r = await window.SRW.runOp('organize_apply', { studio_id: studio.studio_id, file_ids: [x.file_id], kinds: ['check'] });
              const failed = (r.failed || [])[0];
              rowStatus.textContent = r.error ? ` error: ${r.error}` : failed ? ` failed: ${failed.reason}` : ' done';
              if (!r.error && !failed) showPlan(studio, true);
            }),
          }));
        }
      }
      return window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, parts), {});
    }

    // One list of the plan: the pane of a tab (v0.240.14; it was a fold-away panel).
    function group(label, items, kind, studio, hint) {
      const g = el('div', { className: 'srw-tab-pane-body' });
      if (hint) g.appendChild(el('p', { className: 'srw-hint', textContent: hint }));
      if ((kind === 'rename' || kind === 'move') && items.length) {
        const bulkStatus = el('span', { className: 'srw-key-status' });
        g.appendChild(el('div', { className: 'srw-toolbar' }, [
          el('button', {
            className: 'srw-btn srw-btn-primary', textContent: `${kind === 'rename' ? 'Rename' : 'Move'} all ${items.length}`,
            onClick: (e) => confirmInline(e.currentTarget, `${kind === 'rename' ? 'Rename' : 'Move'} ${items.length} file(s)? Stash keeps each scene.`, async () => {
              bulkStatus.textContent = ' working -- the folders are rescanned afterwards, this can take a while…';
              const r = await window.SRW.runOp('organize_apply', {
                studio_id: studio.studio_id, file_ids: items.map((x) => x.file_id), kinds: [kind],
              });
              bulkStatus.textContent = r.error ? ` error: ${r.error}`
                : ` done ${(r.done || []).length}, failed ${(r.failed || []).length} (details in History); Stash scans in the background`;
              showPlan(studio, true);
            }),
          }),
          bulkStatus,
        ]));
      }
      const view = window.SRW.buildFilteredPagedList({
        filters: ['search'], perPage: 50, emptyText: 'None.',
        fetchPage: window.SRW.localPageFetcher(() => items, { text: (x) => `${x.title || ''} ${x.path || ''}` }),
        renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => (kind === 'multi'
          ? window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, [
            el('a', { className: 'srw-release-title', href: `/scenes/${x.scene_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: x.title }),
            el('div', { className: 'srw-review-box' }, [el('span', { className: 'srw-history-message', textContent: x.paths.map(short).join('\n') })]),
            window.SRW.buildStashLink('scene', x.scene_id),
          ]), {})
          : itemRow(x, kind, studio)))),
      });
      g.appendChild(view.node);
      view.reset();
      return g;
    }

    const planYears = {}; // studio id -> chosen year, kept across re-plans
    // The plan of a studio is kept by the sidecar (v0.240.27, table organize_plan) until a rename / move or a reload changes it: opening "Details…" reads it,
    // nothing is asked of StashDB; "Reload" makes a new one. A rename / move takes its files out of the stored plan on the server. The studio's row in
    // the list is read again from there (cheap), so its numbers and the badge follow.
    async function showPlan(studio, keepStatus, fresh = false) {
      if (!planModal) {
        planModal = window.SRW.openModal(`Organize: ${studio.studio}`, planSlot, { size: 'modal-xl srw-modal-wide', onClose: () => { planModal = null; planSlot.replaceChildren(); } });
      }
      if (!keepStatus) planSlot.replaceChildren(el('p', { className: 'srw-hint', textContent: fresh ? `Checking ${studio.studio} against StashDB (fingerprints, titles, studios)…` : 'Loading…' }));
      const p = await window.SRW.runOp('organize_plan', { studio_id: studio.studio_id, fresh });
      if (p.error) { planSlot.replaceChildren(el('p', { textContent: `Error: ${p.error}` })); return; }
      const checkedAt = p.checked_at ? new Date(`${p.checked_at.replace(' ', 'T')}Z`).getTime() : Date.now();
      if (studiosVerified) loadStudios(); // the list, its numbers and the badge from the stored plans (before the first Recount that would be the slow rough count)
      // Quick per-studio / per-year view (2026-09-29): the same plan, filtered by the scene's year.
      const years = [...new Set(['rename', 'move', 'conflict', 'needs_check'].flatMap((k) => (p[k] || []).map((x) => (x.date || '').slice(0, 4)).filter(Boolean)))].sort();
      let year = planYears[studio.studio_id] || '';
      if (year && !years.includes(year)) year = '';
      const byYear = (items) => (year ? (items || []).filter((x) => (x.date || '').startsWith(year)) : (items || []));
      const body = el('div');
      // The lists are tabs with their counts (v0.240.14), like the other pages; the chosen tab is remembered.
      const render = () => {
        const specs = [
          ['rename', 'Rename', byYear(p.rename), 'rename', 'In the same folder, fingerprints verified.'],
          ['move', 'Move', byYear(p.move), 'move', 'To another folder, fingerprints verified.'],
          ['conflict', 'Name conflict', byYear(p.conflict), 'conflict',
            'Not touched. The target name is already taken (usually two versions of one scene in a folder). Decide which file to keep.'],
          ['check', 'Check first', byYear(p.needs_check), 'check',
            'Not touched: the fingerprints don\'t match the tag -- the file doesn\'t match the scene it\'s tagged with, and a wrong tag must not end up in the file name. Use the Fingerprint check to fix the tag first.'],
          ['multi', 'Several files', p.multi_file || [], 'multi', 'Not touched: scenes with more than one file.'],
        ];
        const panes = specs.map(([key, label, items, kind, hint]) => ({ key, label: () => `${label} (${items.length})`, pane: group(label, items, kind, studio, hint) }));
        const first = specs.find((sp) => sp[2].length);
        body.replaceChildren(window.SRW.buildPaneTabs(panes, 'srw:organize-plan-tab', first && first[0]), ...panes.map((x) => x.pane));
      };
      const yearSelect = el('select', {
        className: 'srw-input', onChange: (e) => { year = e.target.value; planYears[studio.studio_id] = year; render(); },
      }, [el('option', { value: '', textContent: 'All years' }), ...years.map((y) => el('option', { value: y, textContent: y }))]);
      yearSelect.value = year;
      const nothing = !['rename', 'move', 'conflict', 'needs_check', 'multi_file'].some((k) => (p[k] || []).length);
      planSlot.replaceChildren(...[
        el('h4', { textContent: `${studio.studio} → folder "${p.studio}"` }),
        nothing ? el('p', { className: 'srw-hint', textContent: 'Nothing to organize: with the provider\'s title and date every file of this studio already sits where it belongs. '
          + 'The count in the list is made from the local title and date and does not know that.' }) : null,
        el('div', { className: 'srw-toolbar' }, [el('label', {}, ['Year: ', yearSelect]),
          el('span', { className: 'srw-hint', textContent: ' narrows the lists below and the "all" buttons to one release year' }),
          window.SRW.iconButton('button', 'faRotate', 'Reload', { title: 'Reload: ask StashDB about the fingerprints, titles and studios again (the plan shown was made at the time next to it)',
            onClick: () => showPlan(studio, false, true) }),
          el('span', { className: 'srw-hint', textContent: `checked ${new Date(checkedAt).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}` })]),
        body,
      ].filter(Boolean)); // (replaceChildren would turn a null into the text "null")
      render();
    }

    // The two tools are tabs (v0.240.8), like Movies & groups and Import folder: scenes by studio, and the film files against the movie templates.
    const scenesPane = el('div', {}, [
      el('p', {
        className: 'srw-hint',
        textContent: 'Brings the Whisparr library folder (scenes/) in line with the naming scheme, studio by studio: '
          + 'the scene\'s own studio in StashDB\'s spelling, then title and date. Only files whose fingerprints match '
          + 'their tag are offered. .funscript/.nfo with the same name are renamed along; if the file is the only video '
          + 'in its folder, folder.jpg etc. move with it and the empty folder is removed. Stash keeps every scene '
          + '(rating, history); each change is logged in History. Suggested order: renames first, then moves.',
      }),
      el('div', { className: 'srw-toolbar' }, [
        el('button', { className: 'srw-btn', textContent: 'Load studios', onClick: () => loadStudios() }),
        recountBtn,
        status,
      ]),
      studioView.node,
    ]);
    try {
      const last = JSON.parse(window.localStorage.getItem(STUDIOS_KEY) || 'null');
      if (last && Array.isArray(last.studios)) { studios = last.studios; studiosVerified = Boolean(last.verified); showStudios(last.at); }
    } catch (e) { /* nothing remembered */ }
    // A Recount that is still running (the page was reloaded meanwhile) is picked up again.
    window.SRW.runOp('organize_recount_status').then((st) => { if (st && st.running) pollRecount(); }).catch(() => {});
    const moviesPane = buildOrganizeMoviesSection();
    const panes = [
      { key: 'scenes', label: 'Scenes by studio', pane: scenesPane },
      { key: 'movies', label: 'Films vs. movie templates', pane: moviesPane },
    ];
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h3', { textContent: 'Organize library (rename / move)' }),
      window.SRW.buildPaneTabs(panes, 'srw:organize-tab'),
      ...panes.map((x) => x.pane),
    ]);
    return section;
  }

  // Existing films against the movie templates (v0.202.0, api/library_organize.py movies_plan): the film files in the movie folders that do not
  // sit where Settings > Import & Naming > Movies/Groups would put them. Rename / move per row or all at once; every row can be played first.
  function buildOrganizeMoviesSection() {
    const { el, confirmInline } = window.SRW;
    const status = el('span', { className: 'srw-key-status' });
    const slot = el('div');
    const short = (p) => p.replace(/^\/data\//, '');

    function itemRow(x, kind) {
      const rowStatus = el('span', { className: 'srw-key-status' });
      const parts = [
        el('a', { className: 'srw-release-title', href: `/scenes/${x.scene_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: x.title }),
        x.diff ? el('span', { className: 'srw-history-category', textContent: x.diff }) : null,
        el('div', { className: 'srw-review-box' }, [
          el('span', { className: 'srw-history-message', textContent: x.new_path ? `${short(x.path)}\n→ ${short(x.new_path)}` : short(x.path) }),
        ]),
        window.SRW.buildStashLink('scene', x.scene_id),
      ];
      if (kind === 'rename' || kind === 'move') {
        parts.push(el('button', {
          className: 'srw-btn srw-btn-sm', textContent: kind === 'rename' ? 'Rename' : 'Move',
          onClick: async (e) => {
            e.target.disabled = true; rowStatus.textContent = ' working…';
            const r = await window.SRW.runOp('organize_movies_apply', { file_ids: [x.file_id], kinds: [kind] });
            const failed = (r.failed || [])[0];
            rowStatus.textContent = r.error ? ` error: ${r.error}` : failed ? ` failed: ${failed.reason}` : ' done';
            if (!r.error && !failed) check(true); else e.target.disabled = false;
          },
        }), rowStatus);
      } else {
        parts.push(el('span', { className: 'srw-release-date', textContent: x.reason || x.check || '' }), rowStatus);
      }
      return window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, parts), {});
    }

    // One list of the check: the pane of a tab (v0.240.14; it was a fold-away panel).
    function group(label, items, kind, hint) {
      const g = el('div', { className: 'srw-tab-pane-body' });
      if (hint) g.appendChild(el('p', { className: 'srw-hint', textContent: hint }));
      if ((kind === 'rename' || kind === 'move') && items.length) {
        // "All at once" works one KIND of difference at a time (year, capitals, title, ...): the kinds differ a lot in how safe a bulk change is.
        const counts = {};
        items.forEach((x) => { counts[x.diff || 'other'] = (counts[x.diff || 'other'] || 0) + 1; });
        const kindSelect = el('select', {}, [
          el('option', { value: '', textContent: 'Apply all of one kind…' }),
          ...Object.keys(counts).sort().map((k) => el('option', { value: k, textContent: `${k} (${counts[k]})` })),
        ]);
        const bulkStatus = el('span', { className: 'srw-key-status' });
        const verb = kind === 'rename' ? 'Rename' : 'Move';
        const bulkBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: `${verb} all of the chosen kind` });
        bulkBtn.disabled = true;
        kindSelect.addEventListener('change', () => {
          bulkBtn.disabled = !kindSelect.value;
          bulkBtn.textContent = kindSelect.value ? `${verb} all ${counts[kindSelect.value]} (${kindSelect.value})` : `${verb} all of the chosen kind`;
        });
        bulkBtn.addEventListener('click', () => confirmInline(bulkBtn, `${verb} ${counts[kindSelect.value]} film file(s) (${kindSelect.value})? Stash keeps each scene and group.`, async () => {
          const chosen = items.filter((x) => (x.diff || 'other') === kindSelect.value);
          bulkStatus.textContent = ' working -- the folders are rescanned afterwards, this can take a while…';
          const r = await window.SRW.runOp('organize_movies_apply', { file_ids: chosen.map((x) => x.file_id), kinds: [kind] });
          bulkStatus.textContent = r.error ? ` error: ${r.error}` : ` done ${(r.done || []).length}, failed ${(r.failed || []).length} (details in History)`;
          check(true);
        }));
        g.appendChild(el('div', { className: 'srw-toolbar' }, [kindSelect, bulkBtn, bulkStatus]));
      }
      const view = window.SRW.buildFilteredPagedList({
        filters: ['search'], perPage: 50, emptyText: 'None.',
        fetchPage: window.SRW.localPageFetcher(() => items, { text: (x) => `${x.title || ''} ${x.path || ''}` }),
        renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => (kind === 'multi'
          ? window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, [
            el('a', { className: 'srw-release-title', href: `/scenes/${x.scene_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: x.title }),
            el('div', { className: 'srw-review-box' }, [el('span', { className: 'srw-history-message', textContent: x.paths.map(short).join('\n') })]),
            window.SRW.buildStashLink('scene', x.scene_id),
          ]), {})
          : itemRow(x, kind)))),
      });
      g.appendChild(view.node);
      view.reset();
      return g;
    }

    // The files of the check open in a modal (v0.240.11); the page keeps the counts and a "Details…" button to open it again.
    const detailSlot = el('div');
    let detailModal = null;
    let lastPlan = null;
    function renderDetails() {
      const p = lastPlan;
      if (!p) return;
      const specs = [
        ['rename', 'Rename', p.rename || [], 'rename', 'In the same folder.'],
        ['move', 'Move', p.move || [], 'move', 'To another folder.'],
        ['conflict', 'Name conflict', p.conflict || [], 'conflict', 'Not touched. The target name is already taken, or two files want the same name. Decide which file to keep.'],
        ['check', 'Check first', p.needs_check || [], 'check',
          'Not touched. A film of several scenes where a scene has no scene number in its group, or a scene in several groups. Fix it in Stash (Group > scene number), then check again.'],
        ['multi', 'Several files', p.multi_file || [], 'multi', 'Not touched: scenes with more than one file.'],
      ];
      const panes = specs.map(([key, label, items, kind, hint]) => ({ key, label: () => `${label} (${items.length})`, pane: group(label, items, kind, hint) }));
      const first = specs.find((sp) => sp[2].length);
      detailSlot.replaceChildren(window.SRW.buildPaneTabs(panes, 'srw:organize-films-tab', first && first[0]), ...panes.map((x) => x.pane));
    }
    function openDetails() {
      if (!lastPlan) return;
      if (!detailModal) {
        detailModal = window.SRW.openModal('Films vs. movie templates', detailSlot, { size: 'modal-xl srw-modal-wide', onClose: () => { detailModal = null; detailSlot.replaceChildren(); } });
      }
      renderDetails();
    }

    async function check(keep) {
      if (!keep) slot.replaceChildren(el('p', { className: 'srw-hint', textContent: 'Checking the film files…' }));
      status.textContent = '';
      const p = await window.SRW.runOp('organize_movies_plan');
      if (p.error) { slot.replaceChildren(el('p', { textContent: `Error: ${p.error}` })); return; }
      const at = Date.now();
      try { window.localStorage.setItem(FILMS_KEY, JSON.stringify({ plan: p, at })); } catch (e) { /* too big or blocked: not remembered */ }
      showSummary(p, at);
      if (detailModal) renderDetails(); // after a rename / move the open list is refreshed
      else if (!keep) openDetails();
    }

    // The last check is kept in the browser (v0.240.12): the page shows it when it is opened again; "Check films" makes a new one.
    const FILMS_KEY = 'srw:organize-films';
    function showSummary(p, at) {
      lastPlan = p;
      const differing = ['rename', 'move', 'conflict', 'needs_check'].reduce((n, k) => n + (p[k] || []).length, 0);
      const counts = [['rename', 'rename'], ['move', 'move'], ['conflict', 'name conflict'], ['needs_check', 'to check first'], ['multi_file', 'with several files']]
        .map(([k, label]) => `${(p[k] || []).length} ${label}`).join(' · ');
      slot.replaceChildren(
        el('p', { className: 'srw-hint', textContent: `${p.in_place || 0} film file(s) already sit where the templates put them; ${differing} differ (${counts}); checked ${new Date(at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}.` }),
        el('div', { className: 'srw-toolbar' }, [window.SRW.iconButton('button', 'faList', 'Details…', { className: 'srw-btn srw-btn-primary',
          title: 'Details: show which files would be renamed / moved, and rename or move them', onClick: openDetails })]),
      );
    }
    try {
      const last = JSON.parse(window.localStorage.getItem(FILMS_KEY) || 'null');
      if (last && last.plan) showSummary(last.plan, last.at);
    } catch (e) { /* nothing remembered */ }

    return el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Movies / groups: existing films vs. the movie templates' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Checks every film file in the movie folders (Movies/, movies/ and the first folder of the movie template) against Settings > Import & Naming > '
          + 'Movies/Groups: title and year come from the Stash group, the part number from the scene number. Rename / move per file, or all files of one kind of difference at once (year, capitals, title, file name ...); Stash keeps the '
          + 'scenes and groups, same-name .nfo/.srt files go along, and a genre folder (e.g. Movies/Anal/) is left when it still holds other files. Nothing changes until you click.',
      }),
      el('div', { className: 'srw-toolbar' }, [
        el('button', { className: 'srw-btn', textContent: 'Check films', onClick: () => check(false) }),
        status,
      ]),
      slot,
    ]);
  }

  // ==================== Import folder (2026-09-29, api/library_import.py) ====================
  function buildImportSection() {
    const { el, confirmInline } = window.SRW;
    const status = el('span', { className: 'srw-key-status' });
    const sourcesSlot = el('div');
    const resultSlot = el('div');
    const short = (p) => p.replace(/^\/data\//, '');
    const picks = {}; // scene_id -> {provider, provider_id} confirmed from the name suggestions

    async function loadSources() {
      status.textContent = ' loading…';
      const r = await window.SRW.runOp('import_sources');
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      status.textContent = '';
      // "Refresh" also checks the analysis on screen: a changed folder (Organized, a move, a new Group) means it is made again (v0.228.4)
      const src = shown && (r.sources || []).find((x) => x.root === shown.root);
      if (shown && !src) { resultSlot.replaceChildren(); shown = null; }
      else if (src && src.signature && src.signature !== shown.signature && !analysing) analyse(shown.root, true);
      restoreAnalysis(r.sources || []);
      sourcesSlot.replaceChildren(el('ul', { className: 'srw-release-list' }, (r.sources || []).map((s) => el('li', { className: 'srw-release-item' }, [
        el('strong', { textContent: s.root }),
        el('span', { className: 'srw-release-date', textContent: s.exists ? `${s.scenes} scene(s) in Stash · ${s.with_id} already identified` : 'folder not found' }),
        s.exists ? el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Analyse', onClick: () => analyse(s.root) }) : null,
        s.exists ? el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Finish source', onClick: () => finishSource(s.root) }) : null,
        el('button', {
          className: 'srw-btn srw-btn-sm', textContent: 'Remove from list',
          title: 'Only takes the folder off this list; Stash\'s libraries and the files stay as they are',
          onClick: async (e) => {
            const rm = await window.SRW.runOp('import_remove', { root: s.root });
            if (rm.error || rm.removed === false) { status.textContent = ` error: ${rm.error || 'not removed'}`; return; }
            resultSlot.replaceChildren();
            forgetAnalysis(s.root);
            loadSources();
          },
        }),
      ]))));
      if (!(r.sources || []).length) sourcesSlot.appendChild(el('p', { className: 'srw-hint', textContent: 'No source folder yet. "Add folder" picks one.' }));
    }

    // Add folder (v0.226.0): browse Stash's own folder tree (the picker of Maintenance's library scope), dry run, then add.
    const addSlot = el('div');
    function openAddFolder() {
      if (addSlot.childNodes.length) { addSlot.replaceChildren(); return; }
      const browserWrap = el('div', { className: 'srw-maintenance-browser' });
      const checkSlot = el('div');
      addSlot.appendChild(el('div', { className: 'srw-maintenance-selective' }, [
        el('div', { className: 'srw-hint', textContent: 'Pick the folder (as Stash sees it) and press "Check this folder".' }), browserWrap, checkSlot]));
      let current = null;

      async function check(path) {
        checkSlot.replaceChildren(el('span', { className: 'srw-hint', textContent: 'Checking…' }));
        const r = await window.SRW.runOp('import_add_check', { path });
        if (r.error) { checkSlot.replaceChildren(el('p', { textContent: `Error: ${r.error}` })); return; }
        const addStatus = el('span', { className: 'srw-key-status' });
        checkSlot.replaceChildren(el('div', {}, [
          el('p', {
            className: 'srw-hint',
            textContent: `${r.path}: ${r.videos} video file(s). ` + (r.can_add
              ? (r.will_add_library ? 'Stash gets this folder as a library path (the old list is saved first) and scans it.'
                : `Stash already scans it (library ${r.covered_by}); only a scan of this folder is started.`)
              : ''),
          }),
          (r.problems || []).length ? el('ul', {}, r.problems.map((p) => el('li', { textContent: p }))) : null,
          r.can_add ? el('div', { className: 'srw-toolbar' }, [
            el('button', {
              className: 'srw-btn srw-btn-primary', textContent: 'Add as import source',
              onClick: (e) => confirmInline(e.currentTarget, `Add ${r.path} as an import source${r.will_add_library ? ' and as a Stash library path' : ''}, then scan it?`, async () => {
                addStatus.textContent = ' working…';
                const f = await window.SRW.runOp('import_add', { path: r.path });
                if (f.error || !f.done) { addStatus.textContent = ` not done: ${f.error || (f.problems || []).join('; ')}`; return; }
                addSlot.replaceChildren();
                status.textContent = ' added -- Stash is scanning it; "Refresh" shows the scenes as they appear';
                loadSources();
              }),
            }),
            addStatus,
          ]) : null,
        ]));
      }

      async function browseTo(path) {
        browserWrap.replaceChildren(el('span', { className: 'srw-hint', textContent: 'Loading…' }));
        const res = await window.SRW.runOp('maintenance_directory_browse', path ? { path } : {});
        if (res.error) { browserWrap.replaceChildren(el('span', { className: 'srw-release-mismatch-warning', textContent: `Failed to browse: ${res.error}` })); return; }
        current = res.path;
        const pathBox = el('input', { type: 'text', readOnly: true });
        pathBox.value = current || '';
        const list = el('ul', { className: 'srw-maintenance-browse-list' });
        if (res.parent) list.appendChild(el('li', {}, [el('button', { className: 'srw-btn srw-btn-sm', textContent: '↑ Up a directory', onClick: () => browseTo(res.parent) })]));
        (res.directories || []).forEach((d) => list.appendChild(el('li', {}, [
          el('button', { className: 'srw-btn srw-btn-sm', textContent: `📁 ${d.split('/').filter(Boolean).pop() || d}`, onClick: () => browseTo(d) })])));
        browserWrap.replaceChildren(
          el('div', { className: 'srw-maintenance-browse-row' }, [pathBox,
            el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Check this folder', onClick: () => check(current) })]),
          list);
      }
      (async () => {
        const libs = await window.SRW.runOp('maintenance_library_paths');
        browseTo(((libs && libs.paths) || [])[0] || null);
      })();
    }

    async function finishSource(root) {
      resultSlot.replaceChildren(el('p', { className: 'srw-hint', textContent: `Checking ${root}…` }));
      const r = await window.SRW.runOp('import_finish_check', { root });
      if (r.error) { resultSlot.replaceChildren(el('p', { textContent: `Error: ${r.error}` })); return; }
      const doneStatus = el('span', { className: 'srw-key-status' });
      const rest = [...(r.videos || []), ...(r.unknown || [])].slice(0, 15);
      resultSlot.replaceChildren(el('div', {}, [
        el('h4', { textContent: `Finish ${root}` }),
        el('p', {
          className: 'srw-hint',
          textContent: `${(r.videos || []).length} video(s), ${(r.junk || []).length} junk file(s) (recycle bin), ${(r.unknown || []).length} other file(s), `
            + `${r.stash_scenes || 0} scene(s) still listed in Stash below it.`,
        }),
        (r.problems || []).length ? el('ul', {}, r.problems.map((p) => el('li', { textContent: p }))) : null,
        rest.length ? el('pre', { className: 'srw-maintenance-file-path', textContent: rest.join('\n') }) : null,
        r.can_finish ? el('div', { className: 'srw-toolbar' }, [
          el('button', {
            className: 'srw-btn srw-btn-primary', textContent: 'Finish: remove from Stash, folder to the recycle bin',
            onClick: (e) => confirmInline(e.currentTarget,
              `Remove ${root} from Stash's libraries and move the empty folder to the recycle bin (deleted after 7 days)?`, async () => {
                doneStatus.textContent = ' working…';
                const f = await window.SRW.runOp('import_finish', { root });
                doneStatus.textContent = f.error ? ` error: ${f.error}` : f.done ? ' done -- the folder is in the recycle bin, the old library list is saved' : ` not done: ${(f.problems || []).join('; ')}`;
                if (f.done) { forgetAnalysis(root); resultSlot.replaceChildren(); loadSources(); }
              }),
          }),
          doneStatus,
        ]) : el('p', { className: 'srw-hint', textContent: 'Not possible yet -- fix the points above first.' }),
      ]));
    }

    async function analyse(root, keepStatus) {
      if (!keepStatus) resultSlot.replaceChildren(el('p', { className: 'srw-hint', textContent: `Analysing ${root} (fingerprint lookups at StashDB and ThePornDB, this takes a minute or two)…` }));
      analysing = true;
      let r;
      try { r = await window.SRW.runOp('import_analyse', { root }); } finally { analysing = false; }
      if (r.error) { resultSlot.replaceChildren(el('p', { textContent: `Error: ${r.error}` })); return; }
      const at = Date.now();
      rememberAnalysis(root, r, at);
      renderAnalysis(root, r, at);
    }

    // The last analysis is kept in this browser (v0.226.2) and shown again when the page is opened; "Analyse" makes a new one.
    const LAST_KEY = 'srw:import-last-analysis';
    // keeps the saved analysis in step with a click that changed a scene (the next visit shows it as it is now)
    function markReviewed(sceneId) {
      try {
        const last = JSON.parse(window.localStorage.getItem(LAST_KEY) || 'null');
        const hit = last && (last.r.items || []).find((i) => String(i.scene_id) === String(sceneId));
        if (hit) { hit.in_review = true; window.localStorage.setItem(LAST_KEY, JSON.stringify(last)); }
      } catch (e) { /* ignore */ }
    }
    function rememberAnalysis(root, r, at) {
      try { window.localStorage.setItem(LAST_KEY, JSON.stringify({ root, r, at })); } catch (e) { /* too big or blocked: just not remembered */ }
    }
    function forgetAnalysis(root) {
      try {
        const last = JSON.parse(window.localStorage.getItem(LAST_KEY) || 'null');
        if (last && last.root === root) window.localStorage.removeItem(LAST_KEY);
      } catch (e) { /* ignore */ }
    }
    function restoreAnalysis(sources) {
      try {
        const last = JSON.parse(window.localStorage.getItem(LAST_KEY) || 'null');
        const src = last && last.r && sources.find((x) => x.root === last.root && x.exists);
        if (!src || resultSlot.childNodes.length) return;
        if (src.signature && last.r.signature === src.signature) { renderAnalysis(last.root, last.r, last.at); return; }
        // Something changed since (a Group, a link, Organized, files): the saved analysis is out of date, so it is made again.
        analyse(last.root);
      } catch (e) { /* ignore */ }
    }

    let shown = null; // {root, signature} of the analysis on screen
    let analysing = false;
    function renderAnalysis(root, r, at) {
      shown = { root, signature: r.signature };
      const c = r.counts || {};
      const tab = (kind, short, pane) => ({ key: kind, label: `${short} (${c[kind] || 0})`, pane });
      const panes = [
        tab('ready', 'Ready', group('Ready to import', r.items.filter((x) => x.kind === 'ready'), 'ready', root,
          'The scene already has a provider id and the fingerprints match. Import = apply the provider\'s metadata (title, date, studio, performers; VR tag for VR files), move to the naming scheme, keep .funscript/.srt/.vtt, junk to the recycle bin.')),
        tab('movie', 'Movies', group('Movie scenes (grouped)', r.items.filter((x) => x.kind === 'movie'), 'movie', root,
          'The scene is in one Stash group that has a ThePornDB movie link (or you marked the scene Organized). It goes to Movies/<Title> (<Year>)/<Title> (<Year>) [- part N] <Quality> -- the layout of the film import; title and year come from the group. Nothing is looked up at the providers and the scene keeps the metadata you gave it. A scene in a group without a link stays under Unknown until you mark it Organized in Stash.')),
        tab('identify', 'Identified', group('Identified by fingerprint', r.items.filter((x) => x.kind === 'identify'), 'identify', root,
          'No id yet, but the provider knows this exact file.')),
        tab('review', 'Needs a look', group('Needs a look', r.items.filter((x) => x.kind === 'review'), 'review', root,
          'Not touched: ambiguous, target name taken, several files in the scene, or a near match of a file older than the release.')),
        tab('unknown', 'Unknown', group('Unknown — name suggestions', r.items.filter((x) => x.kind === 'unknown'), 'unknown', root,
          'No fingerprint match. "Suggest" searches StashDB and ThePornDB with words from the file name; nothing is imported unless you pick a match. Old films with CD parts belong to the movie handling, not here.')),
      ];
      resultSlot.replaceChildren(
        el('h4', { textContent: `${root} — ${c.ready || 0} ready · ${c.movie || 0} movies · ${c.identify || 0} identified by fingerprint · ${c.review || 0} to review · ${c.unknown || 0} unknown` }),
        el('p', { className: 'srw-hint', textContent: `Analysed ${new Date(at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })} (kept from the last visit, nothing has changed since; "Analyse" on the folder makes a new one).` }),
        window.SRW.buildPaneTabs(panes, 'srw:import-tab', ['ready', 'movie', 'identify', 'review', 'unknown'].find((k) => c[k])),
        ...panes.map((x) => x.pane),
      );
    }

    function candidateRow(x, c, rowStatus) {
      const chosen = picks[x.scene_id] && picks[x.scene_id].provider_id === c.provider_id;
      const btn = el('button', {
        className: 'srw-btn srw-btn-sm', textContent: chosen ? 'Picked ✓' : 'Use this',
        onClick: (e) => {
          picks[x.scene_id] = { provider: c.provider, provider_id: c.provider_id };
          e.currentTarget.textContent = 'Picked ✓';
          rowStatus.textContent = ' picked -- press "Import picked" above';
        },
      });
      return el('li', { className: 'srw-release-item' }, [
        el('span', { className: 'srw-release-date', textContent: `${Math.round(c.score * 100)}%` }),
        el('span', { className: 'srw-history-category', textContent: window.SRW.providerLabel(c.provider) }),
        el('span', { textContent: ` ${c.studio || ''} — ${c.title || ''} (${c.date || '?'}${c.minutes ? `, ${c.minutes} min` : ''}) ${c.performers.join(', ')}` }),
        btn,
      ]);
    }

    // The hand tools for a file no fingerprint knows (v0.226.3) are the ones of the other Maintenance pages, not a second set:
    // a MOVIE gets its Group like on Movies & groups ("Make a movie…": create it from a title, or scrape it from a page, e.g. a shop
    // page; movies_create_group), a single SCENE goes to Review: Identify Scene (the tag of Unidentified scenes' "Send to Identify Scene").
    function manualTools(x, rowStatus) {
      const { confirmInline } = window.SRW;
      const guess = (x.title || '').replace(/\.[a-z0-9]{2,4}$/i, '').replace(/[._-]+/g, ' ')
        .replace(/\b(\d{3,4}p|[248]k|hevc|x26[45]|web ?dl|xxx)\b/gi, ' ').replace(/\s+/g, ' ').trim();
      const panel = el('div', { className: 'srw-manual-search-panel' });
      const doneSlot = el('span');
      const doCreate = async (body) => {
        rowStatus.textContent = ' working…';
        let r;
        try { r = await window.SRW.runOp('movies_create_group', { scene_ids: [x.scene_id], ...body }); } catch (e) { r = { error: e.message }; }
        if (r.error) { rowStatus.textContent = ` error: ${r.error}`; return; }
        rowStatus.textContent = '';
        panel.replaceChildren();
        makeBtn.remove(); identBtn.remove();
        doneSlot.replaceChildren(
          el('a', { className: 'srw-btn srw-btn-primary', href: `/groups/${r.group_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: `Open group "${r.title}"` }),
          el('span', { className: 'srw-key-status', textContent: ' The scene is in the group. It counts as a movie here once the group has a ThePornDB movie link or you mark the scene Organized in Stash; then Analyse again.' }));
      };
      const titleInput = el('input', { type: 'text', value: guess, className: 'srw-input' });
      const makeBtn = el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Make a movie…',
        title: 'A film, not a single scene: create its Group (from a title or scraped from a page), like on Movies & groups',
        onClick: () => {
          if (panel.childNodes.length) { panel.replaceChildren(); return; }
          panel.replaceChildren(
            el('div', { className: 'srw-toolbar' }, [titleInput,
              el('button', {
                className: 'srw-btn srw-btn-sm', textContent: 'Create group',
                onClick: (e) => confirmInline(e.currentTarget, `Create the group "${titleInput.value}" with this scene?`, () => doCreate({ title: titleInput.value, group_id: null })),
              })]),
            buildGroupScrape(titleInput.value, doCreate));
        },
      });
      // already sent (the scene carries the tag): a badge instead of a button that would be clicked again (v0.228.5)
      const inReviewBadge = () => el('span', { className: 'srw-history-category', textContent: 'in Review: Identify Scene', title: 'The scene carries the tag; identify it there (Maintenance > Review: Identify Scene)' });
      const identBtn = x.in_review ? inReviewBadge() : window.SRW.iconButton('button', ['faFingerprint', 'faMagnifyingGlass'], 'Send to Identify Scene', {
        className: 'srw-btn srw-btn-sm',
        title: 'A single scene: adds the tag “Review: Identify Scene” (nothing else changes); the tools to identify it are in Maintenance > Review: Identify Scene',
        onClick: async (e) => {
          const res = await window.SRW.runOp('compare_review_add', { scene_id: x.scene_id });
          if (res.error) { rowStatus.textContent = ` error: ${res.error}`; return; }
          x.in_review = true;
          markReviewed(x.scene_id);
          identBtn.replaceWith(inReviewBadge());
          rowStatus.textContent = ' sent (the file stays here until it is identified; then Analyse again)';
        },
      });
      return [window.SRW.buildStashLink('scene', x.scene_id, 'Play in Stash'), makeBtn, identBtn, doneSlot, panel];
    }

    // A file whose Group exists (e.g. scraped from a shop page) but has no ThePornDB movie link yet (v0.227.0): search the movie at ThePornDB and
    // link it -- the link makes the Group a confirmed film for the import; the Group's empty fields are filled from it, nothing that is set changes.
    function tpdbTools(x, rowStatus, root) {
      const { confirmInline } = window.SRW;
      const panel = el('div', { className: 'srw-manual-search-panel' });
      const input = el('input', { type: 'search', className: 'srw-search-input', value: x.title || '' });
      const results = el('div');
      const searchStatus = el('span', { className: 'srw-key-status' });
      async function search(fresh = false) {
        const q = input.value.trim();
        if (!q) return;
        window.SRW.setBusy(searchStatus, 'searching…');
        results.replaceChildren();
        const kept = fresh ? null : window.SRW.recallResult(`tpdb:${x.group_id}:${q}`);
        const res = kept ? kept.value : await window.SRW.runOp('movies_tpdb_search', { q, minutes: x.minutes });
        if (res.error) { searchStatus.textContent = ` error: ${res.error}`; return; }
        if (!kept) window.SRW.keepResult(`tpdb:${x.group_id}:${q}`, res);
        searchStatus.textContent = kept ? `kept from ${new Date(kept.at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}` : '';
        if (!(res.results || []).length) { results.appendChild(el('p', { className: 'srw-hint', textContent: 'ThePornDB found nothing. Try fewer words.' })); return; }
        res.results.forEach((c) => results.appendChild(el('div', { className: 'srw-manual-search-result' }, [
          el('span', { className: 'srw-release-title', textContent: c.title || '?' }),
          el('span', { className: 'srw-review-candidate-meta', textContent: [c.year || '?', c.site, c.minutes ? `${c.minutes} min` : 'no length', `file ${x.minutes} min`, c.length_ok ? '✓ length matches' : null].filter(Boolean).join(' · ') }),
          c.url ? el('a', { className: 'srw-btn srw-btn-sm', href: c.url, target: '_blank', rel: 'noopener noreferrer', textContent: 'Preview' }) : null,
          el('button', {
            className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Link this movie',
            title: 'Adds the ThePornDB page to the Group and fills the Group fields that are empty; nothing that is set changes',
            onClick: (e) => confirmInline(e.currentTarget, `Link the Group "${x.title}" to the ThePornDB movie "${c.title}" (${c.year || '?'})?`, async () => {
              rowStatus.textContent = ' linking…';
              const r = await window.SRW.runOp('movies_link_tpdb', { group_id: x.group_id, tpdb_id: c.id });
              if (r.error) { rowStatus.textContent = ` error: ${r.error}`; return; }
              rowStatus.textContent = ` linked${(r.filled || []).length ? ` (filled: ${r.filled.join(', ')})` : ''} -- analysing again…`;
              analyse(root, true);
            }),
          }),
        ])));
      }
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') search(true); });
      const btn = el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Complete via ThePornDB…',
        title: 'Search the movie at ThePornDB and link it to this Group; the import then takes the Group for a confirmed film',
        onClick: () => {
          if (panel.childNodes.length) { panel.replaceChildren(); return; }
          panel.replaceChildren(el('span', {}, [input, el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Go', onClick: () => search(true) }), searchStatus]), results);
          search();
        },
      });
      return [window.SRW.buildStashLink('scene', x.scene_id, 'Play in Stash'),
        window.SRW.buildStashLink('group', x.group_id, 'Open group'), btn, panel];
    }

    // The provider has this id but cannot confirm the FILE by fingerprint (no fingerprints stored there, or they differ): after looking at it
    // yourself you can import it anyway (v0.228.0); it is placed like a verified one and History records that you confirmed it.
    function trustButton(x, rowStatus, root) {
      const provider = window.SRW.providerLabel(x.provider);
      const meta = [x.provider_studio, x.provider_date].filter(Boolean).join(', ');
      const writes = x.provider_title ? `${provider} says: "${x.provider_title}"${meta ? ` (${meta})` : ''}. ` : '';
      return el('button', {
        className: 'srw-btn srw-btn-sm', textContent: 'Import anyway (I checked it)',
        title: `The check says: ${x.how}. Only if you have looked at the file and it is this scene.`,
        onClick: (e) => window.SRW.confirmInline(e.currentTarget,
          `${x.how}. The scene is "${x.title}". ${writes}Import applies the ${provider} title, date, studio and performers of the id this scene carries now -- they REPLACE what the scene has (a copy of the current data is saved first) -- and moves the file. Only if that id is the right one.`
          + (/older than/.test(x.how) ? ' A file older than the release can be another release of the scene or another scene.' : ''),
          async () => {
            rowStatus.textContent = ' working…';
            const r = await window.SRW.runOp('import_apply', { root, scene_ids: [x.scene_id], trust: [x.scene_id] });
            const failed = (r.failed || [])[0];
            const moved = (r.done || []).length;
            rowStatus.textContent = r.error ? ` error: ${r.error}` : failed ? ` failed: ${failed.reason}` : moved ? ' done' : ' not moved (the plan changed: analyse again)';
            if (!r.error && !failed && moved) analyse(root, true);
          }),
      });
    }

    function itemRow(x, kind, root) {
      const rowStatus = el('span', { className: 'srw-key-status' });
      const sugSlot = el('ul', { className: 'srw-release-list' });
      const parts = [
        el('a', { className: 'srw-release-title', href: `/scenes/${x.scene_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: x.title }),
        x.vr ? el('span', { className: 'srw-history-category', textContent: 'VR' }) : null,
        el('div', { className: 'srw-review-box' }, [
          el('span', { className: 'srw-history-message', textContent: x.new_path ? `${short(x.path)}\n→ ${short(x.new_path)}` : short(x.path) }),
        ]),
        el('span', { className: 'srw-release-date', textContent: x.how || '' }),
      ];
      if (x.kind === 'ready' || x.kind === 'identify' || x.kind === 'movie') {
        parts.push(el('button', {
          className: 'srw-btn srw-btn-sm', textContent: 'Import',
          onClick: async (e) => {
            e.target.disabled = true; rowStatus.textContent = ' working…';
            const r = await window.SRW.runOp('import_apply', { root, scene_ids: [x.scene_id] });
            const failed = (r.failed || [])[0];
            rowStatus.textContent = r.error ? ` error: ${r.error}` : failed ? ` failed: ${failed.reason}` : ' done';
            if (!r.error && !failed) analyse(root, true); else e.target.disabled = false;
          },
        }), rowStatus);
      } else if (x.kind === 'unknown' || x.kind === 'review') {
        if (x.file_id) {
          parts.push(el('button', {
            className: 'srw-btn srw-btn-sm', textContent: 'Suggest',
            onClick: async (e) => {
              e.target.disabled = true; window.SRW.setBusy(rowStatus, 'searching…');
              const r = await window.SRW.runOp('import_suggest', { root, scene_ids: [x.scene_id] });
              e.target.disabled = false;
              if (r.error) { rowStatus.textContent = ` error: ${r.error}`; return; }
              const s = (r.suggestions || {})[String(x.scene_id)] || { candidates: [] };
              rowStatus.textContent = s.candidates.length ? ` searched "${s.query}"` : ` nothing found for "${s.query}"`;
              sugSlot.replaceChildren(...s.candidates.map((c) => candidateRow(x, c, rowStatus)));
            },
          }), rowStatus);
        }
        parts.push(sugSlot);
        if (x.can_trust) parts.push(trustButton(x, rowStatus, root));
        if (x.file_id) parts.push(...(x.group_id ? (x.kind === 'unknown' ? tpdbTools(x, rowStatus, root) : []) : manualTools(x, rowStatus)));
      }
      return el('li', { className: 'srw-release-item' }, parts);
    }

    function group(label, items, kind, root, hint) {
      const g = el('div', { className: 'srw-import-pane' });
      if (hint) g.appendChild(el('p', { className: 'srw-hint', textContent: hint }));
      const bulkStatus = el('span', { className: 'srw-key-status' });
      if ((kind === 'ready' || kind === 'identify' || kind === 'movie') && items.length) {
        g.appendChild(el('div', { className: 'srw-toolbar' }, [
          el('button', {
            className: 'srw-btn srw-btn-primary', textContent: `Import all ${items.length}`,
            onClick: (e) => confirmInline(e.currentTarget, `Import ${items.length} file(s)? Each is moved to the library; Stash keeps the scene.`, async () => {
              bulkStatus.textContent = ' working -- this can take a while (Stash rescans the folders)…';
              const r = await window.SRW.runOp('import_apply', { root, scene_ids: items.map((x) => x.scene_id) });
              bulkStatus.textContent = r.error ? ` error: ${r.error}`
                : ` done ${(r.done || []).length}, failed ${(r.failed || []).length}${(r.leftovers || []).length ? `, ${r.leftovers.length} folder(s) with leftovers` : ''} (details in History)`;
              analyse(root, true);
            }),
          }),
          bulkStatus,
        ]));
      }
      if (kind === 'unknown' && items.length) {
        g.appendChild(el('div', { className: 'srw-toolbar' }, [
          el('button', {
            className: 'srw-btn', textContent: 'Import picked',
            onClick: async (e) => {
              const ids = Object.keys(picks);
              if (!ids.length) { bulkStatus.textContent = ' nothing picked yet'; return; }
              e.target.disabled = true; bulkStatus.textContent = ` importing ${ids.length} picked file(s)…`;
              const r = await window.SRW.runOp('import_apply', { root, scene_ids: ids, picks });
              e.target.disabled = false;
              bulkStatus.textContent = r.error ? ` error: ${r.error}`
                : ` done ${(r.done || []).length}, failed ${(r.failed || []).length} (details in History)`;
              ids.forEach((i) => delete picks[i]);
              analyse(root, true);
            },
          }),
          bulkStatus,
        ]));
      }
      const view = window.SRW.buildFilteredPagedList({
        filters: ['search'], perPage: 50, emptyText: 'None.',
        fetchPage: window.SRW.localPageFetcher(() => items, { text: (x) => `${x.title || ''} ${x.path || ''}` }),
        renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => itemRow(x, kind, root))),
      });
      g.appendChild(view.node);
      view.reset();
      return g;
    }

    const section = el('div', { className: 'srw-settings-section' }, [
      el('h3', { textContent: 'Import folder' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Brings the files of the temporary source folders (the list below; "Add folder" picks one, also a folder Stash does not know yet) into the library: '
          + 'identify by fingerprint at StashDB/ThePornDB, apply the provider\'s metadata (VR files get the "Virtual Reality" tag), '
          + 'move to the naming scheme. Only .funscript/.srt/.vtt move along; jpg/nfo/txt/url/rar go to the recycle bin (7 days); '
          + 'empty folders are removed, the source folder itself stays. Files no fingerprint knows stay put until you pick a match.',
      }),
      el('div', { className: 'srw-toolbar' }, [
        window.SRW.iconButton('button', 'faRotate', 'Reload', { className: 'srw-btn', title: 'Reload: ask again, the list shown is a kept one', onClick: loadSources }),
        el('button', { className: 'srw-btn', textContent: 'Add folder', onClick: openAddFolder }),
        status,
      ]),
      addSlot,
      sourcesSlot,
      resultSlot,
    ]);
    loadSources(); // the list loads itself; the last analysis comes back with it
    return section;
  }

  // ==================== Movies & groups (2026-09-29, api/library_movies.py) ====================
  // A Stash list that loads itself when it is opened and again when it is older than a minute (v0.169.0); "Refresh" loads it at once.
  // loadFn() returns the status text on success or false after it has shown an error itself; the load time is added to the text.
  function autoLoader(statusEl, loadFn) {
    let at = 0; let busy = false;
    return async function run(force = false) {
      if (busy || (!force && at && Date.now() - at < 60000)) return;
      busy = true;
      statusEl.textContent = ' loading…';
      try {
        const text = await loadFn();
        if (text !== false) {
          at = Date.now();
          statusEl.textContent = `${text} (loaded ${new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })})`;
        }
      } finally { busy = false; }
    };
  }

  // Unidentified scenes (v0.170.0): scenes without any provider id outside the film folders. "Check fingerprints" is a DRY RUN (StashDB, exact
  // fingerprints, 40 scenes per request, nothing is written); only the clear matches can then be assigned. Everything else goes to Review >
  // Identify Scene, which has the name search, ThePornDB, playing and the other tools.
  function buildUnidentifiedArea() {
    const { el, confirmInline, notify } = window.SRW;
    let items = [];
    let inReview = 0;
    const results = {}; // scene id -> check result
    let resultFilter = '';
    const status = el('span', { className: 'srw-key-status' });
    const summary = el('div', { className: 'srw-hint' });
    // The "show:" choice: one result class, "any match" (every checked file StashDB knows something about: not "no match"), or "not checked yet".
    const passesResult = (x) => {
      if (!resultFilter) return true;
      const r = results[x.id];
      if (resultFilter === 'unchecked') return !r;
      if (resultFilter === 'matched') return Boolean(r) && r.state !== 'none';
      return Boolean(r) && r.state === resultFilter;
    };
    const STATE_LABEL = { unique: 'clear match', ambiguous: 'several matches', held: 'already in your library', shared: 'two files, one match', none: 'no match' };
    const view = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 50, emptyText: 'No unidentified scenes.',
      fetchPage: window.SRW.localPageFetcher(
        () => items.filter(passesResult),
        { text: (x) => `${x.file} ${x.folder} ${x.title}` }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => {
        const rowStatus = el('span', { className: 'srw-key-status' });
        const r = results[x.id];
        const sdbLink = (m) => el('a', { href: window.SRW.sceneUrl(m.provider || 'stashdb', m.id), target: '_blank', rel: 'noopener noreferrer', textContent: `${m.title || m.id} ↗` });
        const remove = () => { items = items.filter((i) => i.id !== x.id); view.reload(); updateSummary(); };
        const review = window.SRW.iconButton('button', ['faFingerprint', 'faMagnifyingGlass'], 'Send to Identify Scene', {
          title: 'Adds the tag “Review: Identify Scene” (nothing else changes); the tools to identify it are there',
          onClick: async () => {
            const res = await window.SRW.runOp('compare_review_add', { scene_id: x.id });
            if (res.error) { rowStatus.textContent = ` error: ${res.error}`; return; }
            remove();
          } });
        // The row layout of the Wishlist / Stuck lists (v0.238.7): the text on the left, the buttons in one column on the right, never wrapped.
        return window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, [
          el('span', { className: 'srw-release-title', textContent: x.title || x.file }),
          el('span', { className: 'srw-release-date', textContent: [x.studio_folder, x.minutes ? `${x.minutes} min` : null, x.height ? `${x.height}p` : null, x.mb ? `${x.mb} MB` : null].filter(Boolean).join(' · ') }),
          el('div', { className: 'srw-hint', textContent: x.folder }),
          r ? el('div', {}, [el('span', { className: 'srw-history-category', textContent: STATE_LABEL[r.state] || r.state }),
            ...r.matches.map((m) => el('span', {}, [' ', sdbLink(m)])),
            r.held_by ? el('span', { className: 'srw-hint', textContent: ` scene ${r.held_by} has it` }) : null]) : null,
          window.SRW.buildStashLink('scene', x.id, 'Play in Stash'),
          r && r.state === 'unique' ? el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Assign',
            onClick: (e) => confirmInline(e.currentTarget, `Put the ${window.SRW.providerLabel(r.matches[0].provider || 'stashdb')} scene “${r.matches[0].title}” on this file? Title, date, studio, performers and tags are filled in; the old state is saved to the backup file.`, async () => {
              const res = (await window.SRW.runOp('compare_unidentified_apply', { scene_ids: [x.id] })).results || {};
              const one = res[x.id] || {};
              if (one.error) { rowStatus.textContent = ` error: ${one.error}`; return; }
              delete results[x.id]; remove();
            }) }) : null,
          review,
          // A scene that will not be identified (no provider has it) and lies in the wrong folder: park the file like Review's
          // "Move to _UNMATCHED" (v0.228.3). The tag it needs for that is added and taken off by the move itself.
          /\/_UNMATCHED\//.test(`${x.folder}/`) ? null : window.SRW.iconButton('button', ['faFolderOpen', 'faQuestion'], 'Park in _UNMATCHED', {
            className: 'srw-btn srw-btn-sm',
            title: 'Moves the file to scenes/_UNMATCHED/<its folders> until it can be identified; the Stash scene and its data stay (a backup is made)',
            onClick: (e) => confirmInline(e.currentTarget, `Move the file of "${x.title || x.file}" to scenes/_UNMATCHED? The scene and its data stay; a backup is made.`, async () => {
              rowStatus.textContent = ' parking…';
              const tagged = await window.SRW.runOp('compare_review_add', { scene_id: x.id });
              if (tagged.error) { rowStatus.textContent = ` error: ${tagged.error}`; return; }
              const res = await window.SRW.runOp('compare_review_unmatched', { scene_id: x.id });
              if (res.error) { rowStatus.textContent = ` error: ${res.error} (the scene now carries the tag "Review: Identify Scene"; Review: Identify Scene has "Move to _UNMATCHED" too)`; return; }
              remove();
            }),
          }),
          rowStatus,
        ]), {});
      })),
    });
    const filterSel = el('select', { onChange: (e) => { resultFilter = e.target.value; view.reset(); } },
      [['', 'all'], ['matched', 'any match (not “no match”)'], ['unchecked', 'not checked yet'], ['unique', 'clear match'], ['ambiguous', 'several matches'], ['held', 'already in your library'],
        ['shared', 'two files, one match'], ['none', 'no match']].map(([v, t]) => el('option', { value: v, textContent: t })));
    const count = (state) => items.filter((i) => (results[i.id] || {}).state === state).length;
    const assignBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Assign the clear matches', hidden: true });
    function updateSummary() {
      const checked = items.filter((i) => results[i.id]).length;
      const unique = count('unique');
      const when = items.map((i) => (results[i.id] || {}).checked_at).filter(Boolean).sort();
      summary.textContent = `${items.length} scene(s) without a provider id`
        + (inReview ? ` (${inReview} more are already in Review: Identify Scene)` : '')
        + (checked ? ` — checked ${checked} (last ${when[when.length - 1]} UTC, oldest ${when[0].slice(0, 10)}): ${unique} clear, ${count('ambiguous')} several matches, ${count('held')} already in your library, ${count('shared')} two files / one match, ${count('none')} no match` : '');
      assignBtn.hidden = !unique;
      assignBtn.textContent = `Assign the ${unique} clear match(es)`;
    }
    // The list is kept by the sidecar (v0.241.6): opening the page reads it at once; "Reload" asks Stash again. Identified / sent scenes leave it on their own.
    let forceFresh = false;
    async function load() {
      const fresh = forceFresh;
      forceFresh = false;
      const r = await window.SRW.runOp('compare_unidentified', { refresh: fresh });
      if (r.error) { status.textContent = ` error: ${r.error}`; return false; }
      items = r.scenes || [];
      inReview = r.in_review || 0;
      Object.keys(results).forEach((k) => delete results[k]);
      Object.assign(results, r.checks || {}); // the stored results of the last checks (they survive a reload)
      view.reset(); updateSummary();
      return ` ${items.length} scene(s)${r.listed_at ? `, listed ${new Date(`${r.listed_at.replace(' ', 'T')}Z`).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}` : ''}`;
    }
    const loader = autoLoader(status, load);
    const checkBtn = window.SRW.iconButton('button', 'faFingerprint', 'Check fingerprints (dry run)', {
      title: 'Asks StashDB (and ThePornDB for what StashDB does not know) which scene each file is, by exact fingerprint. Nothing is changed.' });
    const runCheck = async (all) => {
      const todo = items.filter((i) => all || !results[i.id]).map((i) => i.id);
      if (!todo.length) { notify('Unidentified scenes', 'Everything listed was checked already.', 'info'); return; }
      checkBtn.disabled = true;
      try {
        for (let i = 0; i < todo.length; i += 120) {
          status.textContent = ` checking ${Math.min(i + 120, todo.length)} / ${todo.length}…`;
          const r = await window.SRW.runOp('compare_unidentified_check', { scene_ids: todo.slice(i, i + 120) });
          if (r.error) { status.textContent = ` error: ${r.error}`; break; }
          Object.assign(results, r.results || {});
          updateSummary(); view.reset();
        }
        status.textContent = ' check done';
      } finally { checkBtn.disabled = false; }
    };
    checkBtn.addEventListener('click', () => runCheck(false));
    const recheckBtn = window.SRW.iconButton('button', 'faArrowsRotate', 'Check everything again', {
      title: 'Asks the providers again for every listed file, also the ones checked before' });
    recheckBtn.addEventListener('click', () => confirmInline(recheckBtn, `Ask the providers again for all ${items.length} files?`, () => runCheck(true)));
    assignBtn.addEventListener('click', () => confirmInline(assignBtn,
      `Assign the matched scene to ${count('unique')} file(s) that matched exactly one scene? Title, date, studio, performers and tags are filled in; each old state is saved to the backup file first.`,
      async () => {
        const ids = items.filter((i) => (results[i.id] || {}).state === 'unique').map((i) => i.id);
        let done = 0; let failed = 0;
        for (let i = 0; i < ids.length; i += 20) {
          status.textContent = ` assigning ${Math.min(i + 20, ids.length)} / ${ids.length}…`;
          const res = (await window.SRW.runOp('compare_unidentified_apply', { scene_ids: ids.slice(i, i + 20) })).results || {};
          Object.entries(res).forEach(([sid, one]) => { if (one.ok) { done += 1; delete results[sid]; items = items.filter((x) => x.id !== sid); } else failed += 1; });
        }
        status.textContent = ` ${done} assigned${failed ? `, ${failed} failed (see History)` : ''}`;
        view.reload(); updateSummary();
      }));
    const bulkReview = window.SRW.iconButton('button', ['faFingerprint', 'faMagnifyingGlass'], 'Send the shown ones to Identify Scene', {
      title: 'Adds the review tag to every scene the current search / filter shows' });
    bulkReview.addEventListener('click', async () => {
      const q = view.state.search || '';
      const shown = items.filter((x) => passesResult(x)
        && (!q || `${x.file} ${x.folder} ${x.title}`.toLowerCase().includes(q.toLowerCase())));
      if (!shown.length) return;
      confirmInline(bulkReview, `Send ${shown.length} scene(s) to Review: Identify Scene?`, async () => {
        for (const x of shown) {
          status.textContent = ` sending ${shown.indexOf(x) + 1} / ${shown.length}…`;
          const res = await window.SRW.runOp('compare_review_add', { scene_id: x.id });
          if (!res.error) items = items.filter((i) => i.id !== x.id);
        }
        status.textContent = ' done'; view.reload(); updateSummary();
      });
    });
    const pane = el('div', {}, [
      el('p', { className: 'srw-hint', textContent: 'Files in your scene folders that Stash has no StashDB / ThePornDB id for (the film folders and the import folder are left out). '
        + '“Check fingerprints” asks StashDB (and ThePornDB, for what StashDB does not know) which scene each file is, by exact fingerprint, and changes nothing (the results are kept, also after a reload; files already checked are skipped); only a file that matches exactly ONE scene, which no other file of yours has, can be assigned. '
        + 'Several matches, none or doubtful cases go to Review: Identify Scene (name search, ThePornDB, playing).' }),
      el('div', { className: 'srw-toolbar' }, [window.SRW.iconButton('button', 'faRotate', 'Reload', { title: 'Reload: read the list from Stash again (it is kept until then; scenes you identify or send to Review leave it at once)', onClick: () => { forceFresh = true; loader(true); } }), checkBtn, recheckBtn, assignBtn, bulkReview,
        el('label', {}, ['show: ', filterSel]), status]),
      summary,
      view.node,
    ]);
    loader();
    return pane;
  }

  function buildPerformersArea() {
    const { el, confirmInline } = window.SRW;
    let performers = [];
    let hideWatchlist = false;
    const wk = window.SRW.watchKind(watchSource); // the watchlist (eye) or Stash's favorites (heart), as Settings says
    // Several cards at once (v0.243.20, tester's hint): a checkbox per card, "select this page", "select all N matching" (the search and the hide switch
    // count), a bulk bar like the one of Upgrade wanted. Removing from the watchlist and deleting go one performer after the other.
    const selected = new Set();
    let pageIds = [];
    const shownPerformers = () => {
      const q = ((perfView.state && perfView.state.search) || '').trim().toLowerCase();
      return performers.filter((p) => !(hideWatchlist && p.on_watchlist) && (!q || p.name.toLowerCase().includes(q)));
    };
    const wkWord = watchSource === 'tag' ? 'watchlist' : 'favorites';
    const perfStatus = el('span', { className: 'srw-key-status' });
    // One card per performer (v0.238.3, like the Search page's): the picture, the name linking to Stash, a heart badge + accent frame for the ones on the
    // watchlist (a watchlist performer may well have no scene yet), and the actions: take it off the watchlist, or delete it from Stash.
    function buildCard(p) {
      const note = el('span', { className: 'srw-key-status' });
      const card = el('div', { className: `srw-search-card srw-perf-card${p.on_watchlist ? ' srw-perf-card-watched' : ''}${selected.has(p.id) ? ' srw-perf-card-selected' : ''}` });
      const pick = el('input', { type: 'checkbox', className: 'srw-card-check', title: 'Select this performer', checked: selected.has(p.id) });
      pick.addEventListener('change', () => {
        if (pick.checked) selected.add(p.id); else selected.delete(p.id);
        card.classList.toggle('srw-perf-card-selected', pick.checked);
        syncSelection();
      });
      card.appendChild(pick);
      const img = p.image_path
        ? el('img', { className: 'srw-search-card-image', src: window.SRW.stashUrl(p.image_path), loading: 'lazy', alt: p.name })
        : el('div', { className: 'srw-search-card-image srw-search-card-noimage' });
      card.appendChild(el('a', { href: `/performers/${p.id}`, target: '_blank', rel: 'noopener noreferrer', title: 'Open the performer in Stash (new tab)' }, [img]));
      card.appendChild(el('div', { className: 'srw-search-card-title' }, [
        p.on_watchlist ? el('span', { className: 'srw-perf-watch-badge', title: wk.on }, [window.SRW.icon(wk.badge) || '●']) : null,
        el('a', { href: `/performers/${p.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: p.name + (p.disambiguation ? ` (${p.disambiguation})` : '') }),
      ]));
      if (p.on_watchlist) card.appendChild(el('span', { className: 'srw-history-category', textContent: wk.on }));
      const actions = [];
      if (p.on_watchlist) {
        actions.push(window.SRW.iconButton('button', wk.remove, wk.removeLabel, {
          title: `${wk.removeLabel}: the performer stays in Stash`,
          onClick: async (e) => {
            const b = e.currentTarget;
            b.disabled = true;
            window.SRW.setBusy(note, 'removing…');
            try {
              const res = await window.SRW.runOp('movies_performer_unwatch', { performer_id: p.id });
              if (res.error) { note.textContent = ` error: ${res.error}`; b.disabled = false; return; }
              p.on_watchlist = false;
              perfView.reload(); updateSummary();
            } catch (err) { note.textContent = ` error: ${err.message}`; b.disabled = false; }
          },
        }));
      }
      actions.push(window.SRW.iconButton('button', 'faTrash', 'Delete performer', {
        className: 'srw-btn srw-btn-danger', title: 'Delete the performer from Stash (only possible while it has no scene)',
        onClick: (e) => confirmInline(e.currentTarget,
          `Delete "${p.name}" from Stash${p.on_watchlist ? ' (and so from the watchlist)' : ''}? This cannot be undone.`,
          async () => {
            window.SRW.setBusy(note, 'deleting…');
            const res = await window.SRW.runOp('movies_performer_delete', { performer_id: p.id });
            if (res.error) { note.textContent = ` error: ${res.error}`; return; }
            performers = performers.filter((x) => x.id !== p.id);
            selected.delete(p.id);
            perfView.reload(); updateSummary(); syncSelection();
          }, { danger: true }),
      }));
      card.appendChild(el('div', { className: 'srw-search-card-actions' }, [...actions, note]));
      return card;
    }
    const perfView = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 48, emptyText: 'No performers without scenes.',
      fetchPage: window.SRW.localPageFetcher(() => performers.filter((p) => !(hideWatchlist && p.on_watchlist)), { text: (x) => x.name }),
      renderItems: (page) => { pageIds = page.map((p) => p.id); setTimeout(syncSelection, 0); return el('div', { className: 'srw-search-grid' }, page.map(buildCard)); },
    });
    const summary = el('p', { className: 'srw-hint' });
    // ---- the bulk bar ----
    const selCount = el('span', { className: 'srw-bulk-count' });
    const bulk = el('div', { className: 'srw-bulk-bar', hidden: true });
    const pageBox = el('input', { type: 'checkbox', title: 'Select / unselect everything on this page' });
    pageBox.addEventListener('change', () => { pageIds.forEach((id) => (pageBox.checked ? selected.add(id) : selected.delete(id))); syncSelection(); perfView.reload(); });
    const allLink = el('a', { href: '#', className: 'srw-bulk-all', onClick: (e) => { e.preventDefault(); shownPerformers().forEach((p) => selected.add(p.id)); syncSelection(); perfView.reload(); } });
    const unwatchAllBtn = el('button', { type: 'button', className: 'srw-btn', textContent: wk.removeLabel,
      title: `${wk.removeLabel} for the selected performers that are on it (they stay in Stash)` });
    const deleteAllBtn = el('button', { type: 'button', className: 'srw-btn srw-btn-danger', textContent: 'Delete',
      title: 'Delete the selected performers from Stash (only possible while a performer has no scene)' });
    const clearBtn = el('button', { type: 'button', className: 'srw-btn', textContent: 'Clear selection', onClick: () => { selected.clear(); syncSelection(); perfView.reload(); } });
    bulk.append(el('span', { className: 'srw-bulk-head' }, [selCount, allLink]), el('span', { className: 'srw-bulk-actions' }, [unwatchAllBtn, deleteAllBtn, clearBtn]));
    const selectedPerformers = () => performers.filter((p) => selected.has(p.id));
    function syncSelection() {
      const matching = shownPerformers();
      selCount.textContent = `${selected.size} selected`;
      allLink.textContent = selected.size < matching.length ? `Select all ${matching.length} matching` : '';
      bulk.hidden = selected.size === 0;
      pageBox.checked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
      const onList = selectedPerformers().filter((p) => p.on_watchlist).length;
      unwatchAllBtn.disabled = !onList;
      unwatchAllBtn.textContent = onList ? `${wk.removeLabel} (${onList})` : wk.removeLabel;
      deleteAllBtn.textContent = `Delete (${selected.size})`;
    }
    // One request per performer, one after the other; a failing one stays selected and is counted.
    async function runForSelected(list, op, done) {
      let ok = 0; const errors = [];
      for (let i = 0; i < list.length; i += 1) {
        window.SRW.setBusy(perfStatus, `${i + 1} / ${list.length}…`);
        try {
          const res = await window.SRW.runOp(op, { performer_id: list[i].id });
          if (res.error) errors.push(`${list[i].name}: ${res.error}`); else { ok += 1; done(list[i]); }
        } catch (err) { errors.push(`${list[i].name}: ${err.message}`); }
      }
      perfStatus.textContent = '';
      return { ok, errors };
    }
    unwatchAllBtn.addEventListener('click', (e) => {
      const list = selectedPerformers().filter((p) => p.on_watchlist);
      if (!list.length) return;
      window.SRW.confirmInline(e.currentTarget, `${wk.removeLabel} for ${list.length} performer(s)? They stay in Stash.`, async () => {
        const r = await runForSelected(list, 'movies_performer_unwatch', (p) => { p.on_watchlist = false; selected.delete(p.id); });
        window.SRW.notify('Performers', `${r.ok} taken off the ${wkWord}${r.errors.length ? `, ${r.errors.length} failed (${r.errors[0]})` : ''}.`, r.errors.length ? 'info' : 'success');
        perfView.reload(); updateSummary(); syncSelection();
      });
    });
    deleteAllBtn.addEventListener('click', (e) => {
      const list = selectedPerformers();
      if (!list.length) return;
      window.SRW.confirmInline(e.currentTarget, `Delete ${list.length} performer(s) from Stash${list.some((p) => p.on_watchlist) ? ' (and so from the watchlist)' : ''}? This cannot be undone.`, async () => {
        const gone = new Set();
        const r = await runForSelected(list, 'movies_performer_delete', (p) => { gone.add(p.id); selected.delete(p.id); });
        performers = performers.filter((x) => !gone.has(x.id));
        window.SRW.notify('Performers', `${r.ok} deleted${r.errors.length ? `, ${r.errors.length} failed (${r.errors[0]})` : ''}.`, r.errors.length ? 'info' : 'success');
        perfView.reload(); updateSummary(); syncSelection();
      }, { danger: true });
    });
    function updateSummary() {
      summary.textContent = `${performers.length} performer(s) without scenes, ${performers.filter((p) => p.on_watchlist).length} of them on the ${wkWord} (marked with ${watchSource === 'tag' ? 'an eye' : 'a heart'}).`;
    }
    async function loadPerformers() {
      const r = await window.SRW.runOp('movies_performers_empty');
      if (r.error) { perfStatus.textContent = ` error: ${r.error}`; return false; }
      performers = r.performers || [];
      [...selected].forEach((id) => { if (!performers.some((p) => p.id === id)) selected.delete(id); });
      perfView.reset();
      updateSummary(); syncSelection();
      return '';
    }
    const loader = autoLoader(perfStatus, loadPerformers);
    const hideBox = el('input', { type: 'checkbox', checked: false, onChange: (e) => { hideWatchlist = e.target.checked; perfView.reset(); syncSelection(); } });
    const perfPane = el('div', {}, [
      el('div', { className: 'srw-toolbar' }, [
        window.SRW.iconButton('button', 'faRotate', 'Reload', { className: 'srw-btn', title: 'Reload: ask again, the list shown is a kept one', onClick: () => loader(true) }),
        el('label', {}, [pageBox, ' select this page']),
        el('label', {}, [hideBox, ` hide the ones on the ${wkWord} (they are meant to have no scene yet)`]), perfStatus,
      ]),
      summary,
      bulk,
      perfView.node,
    ]);
    loader(); // loads itself when the page opens
    return perfPane;
  }

  // ==================== Compare groups (v0.213.0, api/group_compare.py) ====================
  // Film folders without a group next to the EMPTY groups they may be (the same film under a slightly different name). Only suggestions:
  // "Add to that group" puts the video(s) in that group, "Not this" hides the pair; nothing happens without a click.
  // Compare groups is a tab of Movies & groups (v0.242.0; it was its own page, and its list of empty groups was the tab "Groups without files").
  // The tab loads when it is shown; `root.srwCount` = the open suggestions + namesakes for the tab label.
  function buildCompareGroupsArea() {
    const { el, confirmInline } = window.SRW;
    let pairs = [];
    let unmatched = [];
    let ignored = 0;
    const status = el('span', { className: 'srw-key-status' });
    const summary = el('p', { className: 'srw-hint' });
    const pairView = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 25, emptyText: 'No video matches an empty group by name.',
      fetchPage: window.SRW.localPageFetcher(() => pairs, { text: (p) => `${p.title} ${p.candidates.map((c) => c.group.name).join(' ')}` }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map(buildPairRow)),
    });
    // Empty groups that have a namesake WITH files (v0.241.9): usually a duplicate group made by a scrape or an import.
    let namesakes = [];
    const namesakeStatus = el('span', { className: 'srw-key-status' });
    const namesakeView = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 25, emptyText: 'No empty group has a namesake with files.',
      fetchPage: window.SRW.localPageFetcher(() => namesakes, { text: (n) => `${n.group.name} ${n.namesakes.map((x) => x.name).join(' ')}` }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((n) => {
        const rowStatus = el('span', { className: 'srw-key-status' });
        const delBtn = el('button', { className: 'srw-btn srw-btn-sm srw-btn-danger', textContent: 'Delete empty group',
          title: 'Deletes the empty group in Stash (the groups with files stay; scenes without a file stay as scenes)',
          onClick: (e) => confirmInline(e.currentTarget, `Delete the empty group "${n.group.name}"? The namesake with files stays.`, async () => {
            rowStatus.textContent = ' deleting…';
            const r = await window.SRW.runOp('movies_group_delete', { group_id: n.group.id });
            if (r.error) { rowStatus.textContent = ` error: ${r.error}`; return; }
            namesakes = namesakes.filter((x) => x.group.id !== n.group.id);
            namesakeView.reload();
            namesakeStatus.textContent = ` "${r.name}" deleted`;
          }, { danger: true }) });
        // "Dismiss": it is another film with a similar name -- the pair stays hidden (the same table as "Not this"), with Undo.
        const dismissBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Dismiss',
          title: 'Not the same film: hide this empty group with these namesakes',
          onClick: async () => {
            const ids = n.namesakes.map((x) => x.id);
            const r = await window.SRW.runOp('movies_group_namesakes_dismiss', { group_id: n.group.id, namesake_ids: ids });
            if (r.error) { rowStatus.textContent = ` error: ${r.error}`; return; }
            namesakes = namesakes.filter((x) => x.group.id !== n.group.id);
            namesakeView.reload();
            window.SRW.dismissedNotice(`Dismissed: ${n.group.name}`, 'Not the same film as its namesake(s).',
              () => window.SRW.runOp('movies_group_namesakes_dismiss', { group_id: n.group.id, namesake_ids: ids, undo: true }), () => load());
          } });
        return window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, [
          el('a', { className: 'srw-release-title', href: `/groups/${n.group.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: n.group.name }),
          el('span', { className: 'srw-release-date', textContent: `${n.group.date || 'no date'} · ${n.group.scenes ? `${n.group.scenes} scene(s) without a file` : 'no scene'}` }),
          el('div', { className: 'srw-hint' }, ['namesake with files: ', ...n.namesakes.flatMap((x, i) => [i ? ', ' : '',
            el('a', { href: `/groups/${x.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: `${x.name}${x.date ? ` (${x.date.slice(0, 4)})` : ''}` }),
            ` — ${x.with_files} file scene(s)${x.exact ? '' : `, similar ${Math.round(x.score * 100)} %`}`])]),
          delBtn, dismissBtn, rowStatus,
        ]), {});
      })),
    });
    const groupView = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 50, emptyText: 'Every empty group has a suggestion.',
      fetchPage: window.SRW.localPageFetcher(() => unmatched, { text: (g) => g.name }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((g) => el('li', { className: 'srw-release-item' }, [
        el('a', { className: 'srw-release-title', href: `/groups/${g.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: g.name }),
        el('span', { className: 'srw-release-date', textContent: g.date || 'no date' }),
        el('span', { className: 'srw-history-category', textContent: g.reason }),
      ]))),
    });

    function buildPairRow(p) {
      const rowStatus = el('span', { className: 'srw-key-status' });
      const candSlot = el('div');
      const doneSlot = el('span');
      const li = el('li', { className: 'srw-release-item' }, [
        el('span', { className: 'srw-release-title', textContent: p.title }),
        el('span', { className: 'srw-release-date', textContent: `${p.year || 'no year'} · ${p.videos.length} video(s) · ${p.minutes} min` }),
        el('div', { className: 'srw-review-box' }, [
          el('span', { className: 'srw-history-message', textContent: `${p.root}/${p.folder}\n${p.videos.map((v) => v.file).join('\n')}` }),
        ]),
        ...p.videos.filter((v) => v.scene_id).map((v, i) => window.SRW.buildStashLink('scene', v.scene_id, p.videos.length > 1 ? `Play part ${i + 1}` : 'Play in Stash')),
        candSlot, doneSlot, rowStatus,
      ]);
      function drawCandidates() {
        candSlot.replaceChildren(...p.candidates.map((c) => {
          const add = el('button', {
            className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Add to that group',
            onClick: (e) => confirmInline(e.currentTarget, `Add ${p.videos.length} scene(s) to the group "${c.group.name}"?`, async () => {
              let tab = null;
              try { tab = window.open('', '_blank'); } catch (err) { /* the link below appears anyway */ }
              rowStatus.textContent = ' working…';
              let r;
              try { r = await window.SRW.runOp('movies_create_group', { scene_ids: p.videos.map((v) => v.scene_id), group_id: c.group.id }); } catch (err) { r = { error: err.message }; }
              if (r.error) { rowStatus.textContent = ` error: ${r.error}`; if (tab) tab.close(); return; }
              rowStatus.textContent = '';
              candSlot.replaceChildren();
              doneSlot.replaceChildren(el('a', { className: 'srw-btn srw-btn-primary', href: `/groups/${r.group_id}`, target: '_blank', rel: 'noopener noreferrer',
                textContent: `Open group "${r.title}"` }), el('span', { className: 'srw-key-status', textContent: ` ${r.scenes} scene(s) in the group` }));
              if (tab) { try { tab.location.href = `/groups/${r.group_id}`; } catch (err) { /* the link is the fallback */ } }
              status.textContent = ' added -- the entry leaves the list on the next refresh';
            }),
          });
          const no = el('button', {
            className: 'srw-btn srw-btn-sm', textContent: 'Not this', title: 'This is another film: do not suggest this group for this folder again',
            onClick: async () => {
              const r = await window.SRW.runOp('movies_compare_groups_ignore', { folder_key: p.key, group_id: c.group.id });
              if (r.error) { rowStatus.textContent = ` error: ${r.error}`; return; }
              const at = p.candidates.indexOf(c);
              p.candidates.splice(at, 1);
              if (!p.candidates.length) { pairs = pairs.filter((x) => x !== p); pairView.reload(); } else drawCandidates();
              window.SRW.dismissedNotice(`Not this: ${c.group.name}`, `Not suggested again for "${p.title}".`,
                () => window.SRW.runOp('movies_compare_groups_ignore', { folder_key: p.key, group_id: c.group.id, undo: true }),
                () => { p.candidates.splice(at, 0, c); if (!pairs.includes(p)) pairs.push(p); pairView.reload(); drawCandidates(); });
            },
          });
          const notes = [`${Math.round(c.score * 100)} % alike`, c.note, c.year_note, c.group.date ? `group date ${c.group.date}` : 'group has no date'].filter(Boolean).join(' · ');
          return el('div', { className: 'srw-manual-search-result' }, [
            el('a', { className: 'srw-release-title', href: `/groups/${c.group.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: c.group.name }),
            el('span', { className: 'srw-review-candidate-meta', textContent: notes }),
            add, no,
          ]);
        }));
      }
      drawCandidates();
      return li;
    }

    async function load(fresh = false) {
      status.textContent = ' loading…';
      const r = await window.SRW.runOp('movies_compare_groups', { refresh: fresh === true });
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      pairs = r.pairs || []; unmatched = r.unmatched_groups || []; ignored = r.ignored || 0;
      root.srwCount = pairs.length; // set before the summary changes: the tab label watches the summary
      status.textContent = keptText(r.kept_at).replace(/^, /, ' ');
      summary.textContent = `${pairs.length} video folder(s) with a possible group, ${unmatched.length} empty group(s) without a suggestion`
        + (ignored ? `, ${ignored} suggestion(s) hidden by "Not this"` : '') + '.';
      pairView.reset(); groupView.reset();
      const nr = await window.SRW.runOp('movies_group_namesakes', { refresh: fresh === true });
      if (nr.error) { namesakeStatus.textContent = ` error: ${nr.error}`; return; }
      namesakes = nr.namesakes || [];
      namesakeStatus.textContent = (namesakes.length ? ` ${namesakes.length} empty group(s) with a namesake${keptText(nr.kept_at)}` : '')
        + (nr.hidden ? `${namesakes.length ? ', ' : ' '}${nr.hidden} dismissed` : '');
      namesakeView.reset();
    }
    const root = el('div', {}, [
      el('p', { className: 'srw-hint', textContent: 'Every group without a file is listed here: next to the video it may belong to, as a duplicate of a group with files, or without any suggestion. A film folder without a group next to the empty groups it may belong to (the same film under a slightly different name). '
        + 'The numbers in the names must be the same, the year is only a hint. Nothing changes until you click.' }),
      el('div', { className: 'srw-toolbar' }, [window.SRW.iconButton('button', 'faRotate', 'Reload', { title: 'Reload: read the movie folders and the groups from Stash again (they are kept until then)', onClick: () => load(true) }), status]),
      summary,
      el('h4', { textContent: 'Possible matches' }), pairView.node,
      el('h4', { textContent: 'Empty groups with a namesake that has files' }),
      el('p', { className: 'srw-hint' }, ['A group without files whose name is the same film as a group WITH files (accents, "Vol.", ", The" and brackets do not count; the numbers must agree): usually a duplicate. ', namesakeStatus]),
      namesakeView.node,
      el('h4', { textContent: 'Empty groups without a suggestion' }),
      el('p', { className: 'srw-hint', textContent: 'No video of the movie folders has a similar name: these groups probably still wait for their file.' }),
      groupView.node,
    ]);
    root.srwLoad = load;
    root.srwSummary = summary;
    return root;
  }

  // "Scrape…" for a movie without a group (v0.212.0), built like the Edit Group page of Stash: the page is read by whichever installed Stash group
  // scraper handles its address. Stash's group scrapers read a page only, so the way TO a page is either a title search at a source of the
  // sidecar's (the select; AdultDVDEmpire today) or a pasted page address. "Preview" opens the page in a new tab; only "Create group"
  // creates anything. `onCreate({scrape_url})` is the row's create call.
  let groupSourcesCache = null;
  function buildGroupScrape(initial, onCreate) {
    const { el, confirmInline } = window.SRW;
    const PAGE = '__page';
    const select = el('select', { className: 'srw-filter-preset-select' });
    const input = el('input', { type: 'search', className: 'srw-search-input', value: initial || '' });
    const goBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Go' });
    const status = el('span', { className: 'srw-key-status' });
    const sitesNote = el('div', { className: 'srw-hint' });
    const results = el('div');
    let sources = [];
    // One result = cover, title, "Preview" (the page itself in a new tab: that is where it reads well) and "Create group" (Stash's scraper reads
    // the page and the group is created with that data).
    const resultRow = (url, title, image, type) => el('div', { className: 'srw-manual-search-result' }, [
      image ? el('img', { className: 'srw-film-thumb srw-film-thumb-sm', src: image, alt: '', loading: 'lazy' }) : null,
      el('span', { className: 'srw-release-title', textContent: title || url }),
      type ? el('span', { className: 'srw-review-candidate-meta', textContent: type }) : null,
      el('a', { className: 'srw-btn srw-btn-sm', href: url, target: '_blank', rel: 'noopener noreferrer', textContent: 'Preview', title: 'Open the page in a new tab' }),
      el('button', {
        className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Create group',
        title: "Stash's group scraper reads this page; the group is created with that data and the scene(s) go in it",
        onClick: (e) => confirmInline(e.currentTarget, title ? `Create the group "${title}" from this page?` : 'Create the group from this page? Name and data come from the scraper.',
          () => onCreate({ scrape_url: url })),
      }),
    ]);
    async function run(fresh = false) {
      const q = input.value.trim();
      if (!q) return;
      results.replaceChildren();
      if (select.value === PAGE) { results.appendChild(resultRow(q, null, null, 'page address')); return; }
      window.SRW.setBusy(status, 'searching…');
      const kept = fresh ? null : window.SRW.recallResult(`groupsearch:${select.value}:${q}`);
      const res = kept ? kept.value : await window.SRW.runOp('movies_group_search', { source: select.value, q });
      if (res.error) { status.textContent = ` error: ${res.error}`; return; }
      if (!kept) window.SRW.keepResult(`groupsearch:${select.value}:${q}`, res);
      status.textContent = kept ? `kept from ${new Date(kept.at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}` : '';
      const rows = res.results || [];
      if (!rows.length) { results.appendChild(el('p', { className: 'srw-hint', textContent: 'Nothing found.' })); return; }
      rows.forEach((hit) => results.appendChild(resultRow(hit.url, hit.title, hit.image, hit.type || 'video on demand')));
    }
    function modeChanged() {
      const page = select.value === PAGE;
      input.placeholder = page ? 'Page address (https://…) of a site one of the scrapers below supports' : 'Title';
      if (page && input.value === (initial || '')) input.value = '';
      results.replaceChildren();
    }
    select.addEventListener('change', modeChanged);
    goBtn.addEventListener('click', () => run(true));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(true); });
    (async () => {
      groupSourcesCache = groupSourcesCache || await window.SRW.runOp('movies_group_sources');
      const info = groupSourcesCache;
      if (info.error) { status.textContent = ` error: ${info.error}`; return; }
      sources = info.sources || [];
      sources.forEach((src) => select.appendChild(el('option', { value: src.key, textContent: src.label })));
      select.appendChild(el('option', { value: PAGE, textContent: 'Page address' }));
      const names = (info.scrapers || []).map((sc) => sc.name).join(', ');
      sitesNote.textContent = `Page addresses work for the group scrapers installed in Stash: ${names || 'none'}.`;
      modeChanged();
      if (sources.length) run(); // the title is the row's own: search it right away
    })();
    return el('div', {}, [el('span', {}, [select, input, goBtn, status]), sitesNote, results]);
  }

  // "listed 18:32" from a kept list's 'YYYY-MM-DD HH:MM:SS' UTC time (v0.241.7)
  const keptText = (at) => (at ? `, listed ${new Date(`${String(at).replace(' ', 'T')}Z`).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })}` : '');

  function buildMoviesSection() {
    const { el, confirmInline } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h3', { textContent: 'Movies & groups' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Only the movie folders (/data/Movies, /data/movies) are looked at -- normal scenes have no group by design. '
          + 'A film is a Group in Stash: one video = one scene + a group of the same title; a film in parts = one group with one scene per part.',
      }),
    ]);
    // The five parts are tabs (v0.154.0), like the sections of Activity, History and the entity panel; the count of a list shows in its tab once loaded.
    const whisparrPane = el('div');
    if (window.SRW_Films && window.SRW_Films.buildWhisparrSyncTools) whisparrPane.appendChild(window.SRW_Films.buildWhisparrSyncTools());
    if (window.SRW_Films && window.SRW_Films.buildWhisparrFileTools) whisparrPane.appendChild(window.SRW_Films.buildWhisparrFileTools());
    if (window.SRW_Films && window.SRW_Films.buildWhisparrHistoryTools) whisparrPane.appendChild(window.SRW_Films.buildWhisparrHistoryTools());
    const seedPane = el('div');
    if (window.SRW_Films && window.SRW_Films.buildSeedTools) seedPane.appendChild(window.SRW_Films.buildSeedTools());

    // -- videos without a group
    let folders = [];
    let noGroupLoaded = false;
    const noGroupStatus = el('span', { className: 'srw-key-status' });
    const noGroupView = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 25, emptyText: 'Nothing to group.',
      fetchPage: window.SRW.localPageFetcher(() => folders, { text: (x) => `${x.title} ${x.folder}` }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => {
        const rowStatus = el('span', { className: 'srw-key-status' });
        const titleInput = el('input', { type: 'text', value: x.title, className: 'srw-input' });
        // After a group exists the row stays and offers it (v0.212.0): the next step (scrape / identify) is done on the group page in Stash.
        const afterCreate = (r, tab) => {
          rowStatus.textContent = '';
          const open = el('a', { className: 'srw-btn srw-btn-primary', href: `/groups/${r.group_id}`, target: '_blank', rel: 'noopener noreferrer',
            textContent: `Open group "${r.title}"` });
          doneSlot.replaceChildren(open, el('span', { className: 'srw-key-status', textContent: ` ${r.scenes} scene(s) in the group` }));
          createBtn.remove(); adeBtn.remove(); if (newBtn) newBtn.remove(); adePanel.replaceChildren(); // removed, not hidden: confirmInline shows its trigger button again afterwards
          if (tab) { try { tab.location.href = `/groups/${r.group_id}`; } catch (e) { /* the link above is the fallback */ } }
          noGroupStatus.textContent = ' group created -- the entry leaves the list on the next refresh';
        };
        const doCreate = async (body) => {
          // The tab is opened NOW, inside the click (a tab opened after the wait is taken for a pop-up and blocked), and sent to the group when it exists.
          let tab = null;
          try { tab = window.open('', '_blank'); } catch (e) { /* the "Open group" link appears anyway */ }
          rowStatus.textContent = ' working…';
          let r;
          try { r = await window.SRW.runOp('movies_create_group', { scene_ids: x.videos.map((v) => v.scene_id), ...body }); } catch (e) { r = { error: e.message }; }
          if (r.error) { rowStatus.textContent = ` error: ${r.error}`; if (tab) tab.close(); return; }
          afterCreate(r, tab);
        };
        const doneSlot = el('span');
        const adePanel = el('div', { className: 'srw-manual-search-panel' });
        const createBtn = el('button', {
          className: 'srw-btn srw-btn-sm', textContent: x.existing_group ? 'Add to that group' : 'Create group',
          onClick: (e) => confirmInline(e.currentTarget, x.existing_group
            ? `Add ${x.videos.length} scene(s) to the existing group "${x.existing_group.name}"?`
            : `Create the group "${titleInput.value}" with ${x.videos.length} scene(s)?`,
          () => doCreate({ title: titleInput.value, group_id: x.existing_group ? x.existing_group.id : null })),
        });
        // A group of the same name can be ANOTHER film (a remake, a reused title): then a new group is created instead of joining it (v0.212.3).
        const newBtn = x.existing_group ? el('button', {
          className: 'srw-btn srw-btn-sm', textContent: 'Create new group',
          title: 'The existing group is another film with the same name: create a separate group for these video(s)',
          onClick: (e) => confirmInline(e.currentTarget, `Create a NEW group "${titleInput.value}" with ${x.videos.length} scene(s), next to the existing one?`,
            () => doCreate({ title: titleInput.value, group_id: null })),
        }) : null;
        const adeBtn = el('button', {
          className: 'srw-btn srw-btn-sm', textContent: 'Scrape…',
          title: 'Find the film at a source (title search or a page address), look at what Stash scrapes from it, and create the group with that data',
          onClick: () => { if (adePanel.childNodes.length) { adePanel.replaceChildren(); return; } adePanel.replaceChildren(buildGroupScrape(titleInput.value, doCreate)); },
        });
        return el('li', { className: 'srw-release-item' }, [
          titleInput,
          el('span', { className: 'srw-release-date', textContent: `${x.year || 'no year'} · ${x.videos.length} video(s) · ${x.minutes} min` }),
          ...x.flags.map((f) => el('span', { className: 'srw-history-category', textContent: f })),
          el('div', { className: 'srw-review-box' }, [
            el('span', { className: 'srw-history-message', textContent: `${x.root}/${x.folder}\n${x.videos.map((v) => v.file).join('\n')}` }),
          ]),
          // to look at the video(s) before deciding what the movie is: one link per file (to the scene in Stash)
          ...x.videos.filter((v) => v.scene_id).map((v, i) => window.SRW.buildStashLink('scene', v.scene_id, x.videos.length > 1 ? `Play part ${i + 1}` : 'Play in Stash')),
          x.existing_group ? el('span', {
            className: 'srw-history-category',
            textContent: `existing group "${x.existing_group.name}" (${x.existing_group.date || 'no date'}, ${x.existing_group.scenes} scene(s))`,
          }) : null,
          createBtn,
          newBtn,
          adeBtn,
          doneSlot,
          rowStatus,
          adePanel,
        ]);
      })),
    });
    // The lists of this page are kept by the sidecar (v0.241.7): opening reads them at once, a group that is created takes them out of date, "Reload" asks Stash again.
    let forceNoGroup = false;
    async function loadNoGroup() {
      const fresh = forceNoGroup;
      forceNoGroup = false;
      const r = await window.SRW.runOp('movies_no_group', { refresh: fresh });
      if (r.error) { noGroupStatus.textContent = ` error: ${r.error}`; return false; }
      folders = r.folders || [];
      noGroupLoaded = true;
      noGroupView.reset();
      return ` ${folders.length} film folder(s) / video(s) without a group${keptText(r.kept_at)}`;
    }
    const noGroupLoader = autoLoader(noGroupStatus, loadNoGroup);
    const noGroupPane = el('div', {}, [
      el('p', { className: 'srw-hint', textContent: 'Videos in the movie folders that are not in a group yet.' }),
      el('div', { className: 'srw-toolbar' }, [window.SRW.iconButton('button', 'faRotate', 'Reload', { title: 'Reload: read the movie folders from Stash again (the list is kept until then)', onClick: () => { forceNoGroup = true; noGroupLoader(true); } }), noGroupStatus]),
      noGroupView.node,
    ]);

    // -- compare groups: every group without a file (matched with a video, a namesake, or without a suggestion)
    const comparePane = buildCompareGroupsArea();

    // -- already in the library (stage D, v0.160.0): wanted movies whose file Whisparr / Stash already have
    let libItems = [];
    let libLoaded = false;
    const libStatus = el('span', { className: 'srw-key-status' });
    const libSummary = el('span', { className: 'srw-hint' });
    const applyClearBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Use the existing file for the clear ones' });
    applyClearBtn.hidden = true;
    const libView = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 25, emptyText: 'Nothing to show.',
      fetchPage: window.SRW.localPageFetcher(() => libItems, { text: (x) => `${x.title} ${x.year || ''}` }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((v) => {
        const rowStatus = el('span', { className: 'srw-key-status' });
        const group = v.groups && v.groups[0];
        const what = v.action === 'link_group' ? `is already in the group "${group.name}" (${group.id}) -- the movie is linked to it` : 'is in Stash without a group -- it is put into the movie\'s group';
        const act = async (force) => {
          rowStatus.textContent = ' working…';
          const r = await window.SRW.runOp('films_library_apply', { film_ids: [v.film_id], force });
          const one = (r.applied || [])[0] || {};
          rowStatus.textContent = r.error ? ` error: ${r.error}` : (one.error ? ` error: ${one.error}` : ` ${one.status}`);
          if (!r.error && !one.error) { libItems = libItems.filter((x) => x.film_id !== v.film_id); keepLibrary(); libView.reload(); }
        };
        return el('li', { className: 'srw-release-item' }, [
          el('span', { className: 'srw-release-title', textContent: `${v.title}${v.year ? ` (${v.year})` : ''}` }),
          el('span', { className: 'srw-history-category', textContent: v.verdict === 'clear' ? 'clear' : 'to decide' }),
          el('span', { className: 'srw-release-date', textContent: `${v.resolution} · ${v.minutes} min · ${v.size_mb} MB` }),
          el('div', { className: 'srw-hint', textContent: `${v.verdict === 'clear' ? 'Length fits' : `ThePornDB says ${v.expected_min ? Math.round(v.expected_min) : '?'} min, the file has ${Math.round(v.minutes)} min`}; the file ${what}. Whisparr entry ${v.whisparr_entry}.` }),
          window.SRW.buildStashLink('scene', v.scene_id),
          group ? window.SRW.buildStashLink('group', group.id, 'Open group') : null,
          el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Use the existing file',
            onClick: (e) => confirmInline(e.currentTarget, v.verdict === 'clear' ? `Use the existing file for "${v.title}"? Nothing is downloaded, moved or deleted.`
              : `The length does not fit. Use the existing file for "${v.title}" anyway? Nothing is downloaded, moved or deleted.`, () => act(v.verdict !== 'clear')) }),
          el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Keep searching', title: 'The file is not good enough (e.g. 480p): the movie stays on the wishlist and leaves this list',
            onClick: async () => { await window.SRW.runOp('films_library_keep', { id: v.film_id }); libItems = libItems.filter((x) => x.film_id !== v.film_id); keepLibrary(); libView.reload(); } }),
          rowStatus,
        ]);
      })),
    });
    // The result of "Check" is kept in the browser (v0.240.12; it reads Whisparr's whole movie list): shown again after a reload, and
    // lowered by what is decided in the list.
    const LIB_KEY = 'films-library';
    const keepLibrary = (other) => window.SRW.keepResult(LIB_KEY, { clear: libItems.filter((x) => x.verdict === 'clear'), manual: libItems.filter((x) => x.verdict !== 'clear'),
      other: other != null ? other : ((window.SRW.recallResult(LIB_KEY) || { value: {} }).value.other || 0) });
    async function loadLibrary(restored) {
      libStatus.textContent = ' checking (reads Whisparr\'s movie list once, can take half a minute)…';
      const r = restored || await window.SRW.runOp('films_library_check');
      if (r.error) { libStatus.textContent = ` error: ${r.error}`; return; }
      if (!restored) window.SRW.keepResult(LIB_KEY, r);
      libItems = [...(r.clear || []), ...(r.manual || [])];
      libLoaded = true;
      const clear = (r.clear || []).length;
      libStatus.textContent = '';
      libSummary.textContent = libItems.length
        ? `${clear} clear, ${(r.manual || []).length} to decide, ${r.other || 0} wanted movie(s) without a file known in Stash.`
        : `Nothing to decide: no wanted movie has a file in the library (${r.other || 0} wanted movie(s) without a file known in Stash).`;
      applyClearBtn.hidden = !clear;
      applyClearBtn.textContent = `Use the existing file for the ${clear} clear one(s)`;
      libView.reset();
    }
    applyClearBtn.addEventListener('click', (e) => confirmInline(e.currentTarget, 'Use the existing file for all clear movies? Nothing is downloaded, moved or deleted; the movies become imported.', async () => {
      libStatus.textContent = ' working…';
      const r = await window.SRW.runOp('films_library_apply', { film_ids: null, force: false });
      libStatus.textContent = r.error ? ` error: ${r.error}` : ` ${(r.applied || []).filter((x) => x.status === 'imported').length} of ${(r.applied || []).length} imported`;
      if (!r.error) await loadLibrary();
    }));
    const libraryPane = el('div', {}, [
      el('p', { className: 'srw-hint', textContent: 'Wanted movies whose file is ALREADY in the library (Whisparr imported it earlier, often under another ThePornDB id) -- they would be downloaded again. “Clear” = the length fits '
        + 'and the scene is in at most one group; “to decide” = the length does not fit (ThePornDB sometimes lists one disc only) or the file is in several groups. Using the existing file downloads, moves and deletes nothing.' }),
      el('div', { className: 'srw-toolbar' }, [window.SRW.iconButton('button', 'faClipboardCheck', 'Check', { className: 'srw-btn', onClick: () => loadLibrary() }), applyClearBtn, libStatus]),
      libSummary,
      libView.node,
    ]);
    libSummary.textContent = 'Click Check to look for wanted movies whose file is already in the library.';
    const lastLibrary = window.SRW.recallResult(LIB_KEY, 30 * 86400000);
    if (lastLibrary && lastLibrary.value) {
      loadLibrary(lastLibrary.value).then(() => { libStatus.textContent = `kept from ${new Date(lastLibrary.at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })} — "Check" asks again`; });
    }

    const panes = [
      { key: 'whisparr', label: 'Whisparr', pane: whisparrPane },
      { key: 'library', label: () => `Already in the library${libLoaded ? ` (${libItems.length})` : ''}`, pane: libraryPane, watch: libSummary },
      { key: 'seed', label: 'Look-up on ThePornDB', pane: seedPane },
      { key: 'no-group', label: () => `Videos without a group${noGroupLoaded ? ` (${folders.length})` : ''}`, pane: noGroupPane, watch: noGroupStatus, onShow: () => noGroupLoader() },
      { key: 'compare', label: () => `Compare groups${comparePane.srwCount != null ? ` (${comparePane.srwCount})` : ''}`, pane: comparePane, watch: comparePane.srwSummary, onShow: () => comparePane.srwLoad() },
    ];
    section.appendChild(window.SRW.buildPaneTabs(panes, 'srw:maintenance-movies-tab'));
    panes.forEach((p) => section.appendChild(p.pane));
    return section;
  }

  function buildUnknownQualitySection() {
    const { el, confirmInline } = window.SRW;
    let items = [];
    const status = el('span', { className: 'srw-key-status' });
    const base = (p) => p.split('/').pop();
    const listSlot = el('div');
    const view = window.SRW.buildFilteredPagedList({
      filters: ['search', 'vr'],
      perPage: 50,
      emptyText: 'No file names with "Unknown" as quality.',
      fetchPage: window.SRW.localPageFetcher(() => items, {
        text: (x) => `${x.title} ${x.path}`, vr: (x) => x.is_vr,
      }),
      renderItems: (page) => el('ul', { className: 'srw-release-list' }, page.map((x) => {
        const rowStatus = el('span', { className: 'srw-key-status' });
        return el('li', { className: 'srw-release-item' }, [
          el('a', {
            className: 'srw-release-title', href: `/scenes/${x.scene_id}`, target: '_blank',
            rel: 'noopener noreferrer', textContent: x.title || `Scene ${x.scene_id}`,
          }),
          el('span', { className: 'srw-release-date', textContent: `${x.width}x${x.height}${x.is_vr ? ' · VR' : ''}` }),
          el('div', { className: 'srw-review-box' }, [
            el('span', { className: 'srw-history-message', textContent: `${base(x.path)}  →  ${base(x.new_path)}` }),
          ]),
          el('button', {
            className: 'srw-btn srw-btn-sm', textContent: 'Rename',
            onClick: async (e) => {
              e.target.disabled = true;
              rowStatus.textContent = ' renaming…';
              const r = await window.SRW.runOp('unknown_quality_apply', { paths: [x.path] });
              rowStatus.textContent = r.error ? ` error: ${r.error}`
                : (r.renamed || []).length ? ' done' : ` failed: ${((r.failed || r.skipped || [])[0] || {}).reason || '?'}`;
              if (!r.error && (r.renamed || []).length) load();
              else e.target.disabled = false;
            },
          }),
          rowStatus,
        ]);
      })),
    });
    async function load() {
      status.textContent = ' loading…';
      const r = await window.SRW.runOp('unknown_quality_list');
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      items = r.items || [];
      status.textContent = ` ${items.length} file(s)`;
      view.reset();
    }
    const renameAllBtn = el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Rename all',
      onClick: (e) => confirmInline(e.currentTarget, `Rename all ${items.length} file(s)? Stash keeps each scene.`,
        async () => {
          status.textContent = ` renaming ${items.length} file(s) and rescanning -- can take a few minutes…`;
          const r = await window.SRW.runOp('unknown_quality_apply', { paths: items.map((x) => x.path) });
          status.textContent = r.error ? ` error: ${r.error}` : ` renamed ${(r.renamed || []).length}, `
            + `failed ${(r.failed || []).length}, skipped ${(r.skipped || []).length} (details in History)`;
          load();
        }),
    });
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h3', { textContent: 'File names with "Unknown" quality' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Replaces only the quality part of the name: the source from the original release name when '
          + 'this sidecar grabbed it, otherwise WEBDL (your library\'s convention for untagged site releases); the '
          + 'resolution from the file itself (VR: width class like 8K). Stash keeps each scene (rating, history) -- '
          + 'the folders are rescanned afterwards. Every rename is logged in History.',
      }),
      el('div', { className: 'srw-toolbar' }, [
        el('button', { className: 'srw-btn', textContent: 'Find files', onClick: () => load() }), renameAllBtn, status,
      ]),
      listSlot,
    ]);
    listSlot.appendChild(view.node);
    return section;
  }

  // ==================== Convert (v0.245.0, api/convert.py) ====================
  // Library files no browser plays directly (old video codec, or good H.264 in an old container) -> H.264 + AAC in an MP4, made by an encode worker
  // (not the NAS). The Stash scene stays; the new file joins it and the old file goes to the recycle bin.
  function buildConvertSection() {
    const { el, confirmInline, notify } = window.SRW;
    const status = el('span', { className: 'srw-key-status' });
    const workerLine = el('p', { className: 'srw-hint' });
    const list = el('div');
    const MODE_TEXT = { reencode: ['Re-encode', 'The video is encoded again (old codec); the picture size stays'],
                        remux: ['Repack', 'The video is already H.264 and is copied as it is; only the container (and maybe the sound) changes -- takes seconds, loses nothing'] };
    const STATES = [['open', 'To convert'], ['active', 'Queued / running'], ['failed', 'Failed'], ['done', 'Converted'], ['dismissed', 'Dismissed']];
    const ACTIVE = ['queued', 'running', 'verifying', 'replacing'];
    const fmtGb = (b) => (b ? (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${Math.round(b / 1e6)} MB`) : '?');
    const fmtMin = (sec) => (sec ? `${Math.round(sec / 60)} min` : '?');
    let rows = [];
    let history = { done: [], dismissed: [] };
    let counts = { done: 0, dismissed: 0 };
    let view = null;
    let timer = null;

    const stateSelect = el('select', { title: 'Which files to show' }, STATES.map(([k, t]) => el('option', { value: k, textContent: t })));
    const modeSelect = el('select', { title: 'Re-encode: old video codec. Repack: good H.264 in an old container.' }, [
      el('option', { value: '', textContent: 'All kinds' }), el('option', { value: 'reencode', textContent: 'Re-encode' }), el('option', { value: 'remux', textContent: 'Repack' })]);
    const SORTS = [['title', 'Title', (c) => (c.scene_title || '').toLowerCase(), true], ['size', 'File size', (c) => c.size || 0, false],
      ['length', 'Length', (c) => c.duration || 0, false], ['format', 'Format', (c) => `${c.container}/${c.video_codec}`, true]];
    const sortSelect = el('select', { title: 'Sort the list' }, SORTS.map(([k, t]) => el('option', { value: k, textContent: `Sort: ${t}` })));
    const sortDir = el('button', { type: 'button', className: 'srw-btn', title: 'Reverse the order', textContent: '↑',
      onClick: () => { reversed = !reversed; sortDir.textContent = reversed ? '↓' : '↑'; view.reset(); } });
    let reversed = false;
    const stateOf = (c) => (c.status === 'failed' ? 'failed' : ACTIVE.includes(c.status) ? 'active' : c.status === 'done' ? 'done' : c.status === 'dismissed' ? 'dismissed' : 'open');
    const pool = () => {
      const st = stateSelect.value;
      const base = st === 'done' ? history.done : st === 'dismissed' ? history.dismissed : rows;
      return base.filter((c) => stateOf(c) === st && (!modeSelect.value || c.mode === modeSelect.value));
    };
    const sorted = () => {
      const def = SORTS.find((x) => x[0] === sortSelect.value);
      const asc = def[3] !== reversed;
      return pool().sort((a, b) => { const x = def[2](a); const y = def[2](b); const d = typeof x === 'string' ? x.localeCompare(y) : x - y; return asc ? d : -d; });
    };

    // ---- selection ----
    const selected = new Set();
    let pageIds = [];
    const selCount = el('span', { className: 'srw-bulk-count' });
    const bulk = el('div', { className: 'srw-bulk-bar', hidden: true });
    const pageBox = el('input', { type: 'checkbox', title: 'Select / unselect everything on this page' });
    pageBox.addEventListener('change', () => { pageIds.forEach((id) => (pageBox.checked ? selected.add(id) : selected.delete(id))); syncSelection(); view.reload(); });
    const allLink = el('a', { href: '#', className: 'srw-bulk-all', onClick: (e) => { e.preventDefault(); sorted().forEach((c) => selected.add(c.id)); syncSelection(); view.reload(); } });
    function syncSelection() {
      selCount.textContent = `${selected.size} selected`;
      const matching = sorted();
      allLink.textContent = selected.size < matching.length ? `Select all ${matching.length} matching` : '';
      bulk.hidden = selected.size === 0;
      pageBox.checked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
    }
    const selectedRows = () => pool().filter((c) => selected.has(c.id));
    async function bulkOp(op, ids, done) {
      const r = await window.SRW.runOp(op, { ids });
      if (r.error) { notify('Convert', r.error, 'error'); return null; }
      selected.clear();
      await load();
      if (done) notify('Convert', done(r), 'success');
      return r;
    }
    const estimate = (cs) => {
      const re = cs.filter((c) => c.mode === 'reencode');
      const minutes = Math.round(re.reduce((a, c) => a + (c.duration || 0), 0) / 60 / 4); // rough: an old low-resolution file encodes at about 4x
      return `${cs.length} file(s), ${re.length} to re-encode (roughly ${minutes < 90 ? `${minutes} min` : `${(minutes / 60).toFixed(1)} h`} of encoding at 4x), ${cs.length - re.length} to repack`;
    };
    const convertBtn = el('button', { type: 'button', className: 'srw-btn srw-btn-primary', textContent: 'Convert', title: 'Queue the selected files; an encode worker converts them one after the other',
      onClick: (e) => confirmInline(e.currentTarget,
        `Convert ${estimate(selectedRows())}? The scene stays, the converted file joins it and the old file goes to the recycle bin. Needs an encode worker.`,
        () => bulkOp('convert_queue', [...selected], (r) => `${r.queued} file(s) queued`)) });
    const cancelBtn = el('button', { type: 'button', className: 'srw-btn', textContent: 'Cancel', title: 'Take the selected queued / running files out of the queue (a running conversion is stopped)',
      onClick: () => bulkOp('convert_cancel', [...selected], (r) => `${r.cancelled} file(s) taken out of the queue`) });
    const dismissBtn = el('button', { type: 'button', className: 'btn btn-outline-danger srw-btn-fit', textContent: 'Dismiss', title: 'Keep these files as they are; they leave the list',
      onClick: (e) => confirmInline(e.currentTarget, `Dismiss ${selected.size} file(s)? They stay as they are and are not offered again (restore them under Dismissed).`,
        () => bulkOp('convert_dismiss', [...selected], (r) => `${r.changed} file(s) dismissed`)) });
    const restoreBtn = el('button', { type: 'button', className: 'srw-btn', textContent: 'Restore', title: 'Offer the selected dismissed files again',
      onClick: async () => { const r = await window.SRW.runOp('convert_dismiss', { ids: [...selected], restore: true }); if (r.error) { notify('Convert', r.error, 'error'); return; } selected.clear(); load(); } });
    const clearBtn = el('button', { type: 'button', className: 'srw-btn', textContent: 'Clear selection', onClick: () => { selected.clear(); syncSelection(); view.reload(); } });
    bulk.append(selCount, allLink, convertBtn, cancelBtn, dismissBtn, restoreBtn, clearBtn);
    function syncBulkButtons() {
      const st = stateSelect.value;
      convertBtn.hidden = !(st === 'open' || st === 'failed');
      cancelBtn.hidden = st !== 'active';
      dismissBtn.hidden = !(st === 'open' || st === 'failed');
      restoreBtn.hidden = st !== 'dismissed';
    }

    // ---- the data ----
    const refreshBtn = el('button', { type: 'button', className: 'srw-btn', textContent: 'Find candidates',
      title: 'Reads every file from Stash and lists those that no browser plays directly (nothing is changed). Files already in Upgrade wanted are left out.',
      onClick: async () => {
        refreshBtn.disabled = true; window.SRW.setBusy(status, 'reading Stash…');
        const r = await window.SRW.runOp('convert_refresh');
        refreshBtn.disabled = false; status.textContent = '';
        if (r.error) { notify('Convert', r.error, 'error'); return; }
        if (r.skipped) { notify('Convert', r.reason, 'info'); return; }
        notify('Convert', `${r.wanted} file(s) to convert (${r.reencode} to re-encode, ${r.remux} to repack); ${r.added} new, ${r.removed} gone.`, 'success');
        load();
      } });
    async function loadHistory(kind) {
      const r = await window.SRW.runOp('convert_history', { status: kind });
      if (!r.error) history[kind] = r.rows || [];
    }
    async function load() {
      const r = await window.SRW.runOp('convert_list');
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      rows = r.rows || [];
      counts = { done: r.done, dismissed: r.dismissed };
      if (stateSelect.value === 'done' || stateSelect.value === 'dismissed') await loadHistory(stateSelect.value);
      const w = r.worker || {};
      workerLine.textContent = (w.online ? `Encode worker online${w.name ? `: ${w.name}` : ''}.` : `No encode worker is asking right now${w.name ? ` (last: ${w.name}, ${w.seconds_ago == null ? '' : `${Math.round(w.seconds_ago / 60)} min ago`})` : ''} -- queued files wait for one.`)
        + (r.media_path_set ? '' : ' The folder of the library on the worker machine is not set yet (Settings > Quality > "Library on the worker"); without it the worker needs --map /media=<folder>.');
      workerLine.className = w.online && r.media_path_set ? 'srw-hint' : 'srw-review-warning';
      [...stateSelect.options].forEach((o) => {
        const n = o.value === 'done' ? counts.done : o.value === 'dismissed' ? counts.dismissed : rows.filter((c) => stateOf(c) === o.value).length;
        o.textContent = `${STATES.find((x) => x[0] === o.value)[1]} (${n})`;
      });
      for (const id of [...selected]) if (!rows.concat(history.done, history.dismissed).some((c) => c.id === id)) selected.delete(id);
      if (!view) {
        view = window.SRW.buildFilteredPagedList({
          filters: ['search'], perPage: 30, showFilterBarFrom: 10,
          emptyText: 'Nothing here (click "Find candidates" to read the library).',
          fetchPage: window.SRW.localPageFetcher(() => sorted(), { text: (c) => `${c.scene_title} ${c.file_path}` }),
          renderItems: (items) => { pageIds = items.map((c) => c.id); syncSelection();
            return el('ul', { className: 'srw-release-list srw-upgrade-list' }, [el('li', { className: 'srw-upgrade-selectall' }, [el('label', {}, [pageBox, ' select this page'])]), ...items.map(buildRow)]); },
        });
        list.appendChild(view.node);
        [stateSelect, modeSelect].forEach((s) => s.addEventListener('change', async () => {
          if (s === stateSelect && (s.value === 'done' || s.value === 'dismissed')) await loadHistory(s.value);
          selected.clear(); syncBulkButtons(); view.reset(); }));
        sortSelect.addEventListener('change', () => view.reset());
      }
      syncBulkButtons();
      view.reload();
      syncSelection();
      // Progress: asked again every few seconds while something is queued or running (and only while this page is on screen).
      const busy = rows.some((c) => ACTIVE.includes(c.status));
      if (busy && !timer) timer = setInterval(() => { if (!section.isConnected) { clearInterval(timer); timer = null; return; } load(); }, 5000);
      if (!busy && timer) { clearInterval(timer); timer = null; }
    }

    function buildRow(c) {
      const [modeLabel, modeTitle] = MODE_TEXT[c.mode] || [c.mode, ''];
      const cb = el('input', { type: 'checkbox', title: 'Select' });
      cb.checked = selected.has(c.id);
      cb.addEventListener('change', () => { if (cb.checked) selected.add(c.id); else selected.delete(c.id); syncSelection(); });
      const facts = [`${c.container}/${c.video_codec}/${c.audio_codec || 'no sound'}`, c.width && c.height ? `${c.width}x${c.height}` : null, fmtGb(c.size), fmtMin(c.duration),
        c.bit_rate ? `${(c.bit_rate / 1e6).toFixed(1)} Mbit/s` : null].filter(Boolean);
      const note = {
        wanted: '', failed: c.error || 'failed', queued: 'waiting for an encode worker', verifying: 'checking the converted file…', replacing: 'putting the converted file into the scene…',
        running: `${c.worker || 'worker'}: ${c.percentage || 0} %${c.speed_x ? ` (${c.speed_x}x)` : ''}${c.seconds_left ? `, about ${Math.max(1, Math.round(c.seconds_left / 60))} min left` : ''}`,
        done: `converted: ${fmtGb(c.size)} -> ${fmtGb(c.size_after)}; the old file is in the recycle bin`, dismissed: 'dismissed -- the file is kept as it is',
      }[c.status] || c.status;
      const actions = [];
      if (c.status === 'wanted' || c.status === 'failed') {
        actions.push(el('button', { type: 'button', className: 'srw-btn srw-btn-primary', textContent: 'Convert', title: 'Queue this file for an encode worker',
          onClick: (e) => confirmInline(e.currentTarget, `Convert "${c.scene_title || c.file_path.split('/').pop()}" (${modeLabel.toLowerCase()})? The scene stays and the old file goes to the recycle bin.`,
            async () => { const r = await window.SRW.runOp('convert_queue', { ids: [c.id] }); if (r.error) { notify('Convert', r.error, 'error'); return; } load(); }) }));
        actions.push(el('button', { type: 'button', className: 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss', textContent: 'Dismiss', title: 'Keep the file as it is',
          onClick: async () => { await window.SRW.runOp('convert_dismiss', { ids: [c.id] }); await load();
            window.SRW.dismissedNotice(`Dismissed: ${c.scene_title || 'file'}`, 'The file is kept as it is.', () => window.SRW.runOp('convert_dismiss', { ids: [c.id], restore: true }), () => load()); } }));
      } else if (c.status === 'queued' || c.status === 'running') {
        actions.push(el('button', { type: 'button', className: 'srw-btn', textContent: 'Cancel', title: c.status === 'running' ? 'Stop this conversion' : 'Take it out of the queue',
          onClick: async () => { await window.SRW.runOp('convert_cancel', { ids: [c.id] }); load(); } }));
      } else if (c.status === 'dismissed') {
        actions.push(el('button', { type: 'button', className: 'srw-btn', textContent: 'Restore', onClick: async () => { await window.SRW.runOp('convert_dismiss', { ids: [c.id], restore: true }); load(); } }));
      }
      return el('li', { className: 'srw-release-row srw-upgrade-row' }, [
        el('div', { className: 'srw-rr-main' }, [
          el('div', {}, [cb, ' ', el('a', { href: `/scenes/${c.scene_id}`, textContent: c.scene_title || c.file_path.split('/').pop() }), ' ',
            el('span', { className: 'srw-chip', textContent: modeLabel, title: modeTitle })]),
          el('div', { className: 'srw-hint', textContent: c.file_path }),
          el('div', { className: 'srw-chips' }, facts.map((t) => el('span', { className: 'srw-chip', textContent: t }))),
          note ? el('div', { className: c.status === 'failed' ? 'srw-review-warning' : 'srw-hint', textContent: note }) : null,
        ]),
        el('div', { className: 'srw-rr-actions' }, [window.SRW.buildStashLink('scene', c.scene_id, 'Play in Stash'), ...actions]),
      ]);
    }

    const section = el('div', { className: 'srw-panel srw-convert' }, [
      el('p', {
        className: 'srw-hint',
        textContent: 'Files that no browser plays directly: an old video codec (MPEG-4 in AVI, WMV, FLV, MPEG-1 ...) is encoded again as H.264 + AAC in an MP4 of the same size; good H.264 in an old container (FLV ...) is only repacked. '
          + 'The work is done by an encode worker on another machine (not the NAS). The Stash scene stays: the converted file joins it, and the old file goes to the recycle bin. HEVC and VR files and files that only have AC3 sound are not part of this page.',
      }),
      workerLine,
      el('div', { className: 'srw-toolbar' }, [refreshBtn, stateSelect, modeSelect, sortSelect, sortDir, status]),
      bulk,
      list,
    ]);
    load();
    return section;
  }

  function buildUpgradeSection() {
    const { el, confirmInline, notify } = window.SRW;
    const status = el('span', { className: 'srw-key-status' });
    const list = el('div');
    // ---- filters: why (reason), where it stands (state), without id ----
    // Filter by the reason, with the resolution of the current file split out for "below cutoff" (by frame size, so a cropped 1920×800
    // file counts as 1080p). Scenes without a StashDB / ThePornDB id cannot be searched yet and are hidden unless asked for.
    const resBucket = (c) => {
      const w = c.cur_width || 0; const h = c.cur_height || 0;
      const eff = Math.max(Math.min(w, h), Math.round(Math.max(w, h) * 9 / 16));
      return eff < 470 ? 'lt480' : eff < 700 ? '480' : eff < 1000 ? '720' : eff < 1400 ? '1080' : eff < 2000 ? '1440' : '2160';
    };
    const BUCKETS = [['lt480', 'under 480p'], ['480', '480p'], ['720', '720p'], ['1080', '1080p'], ['1440', '2K (1440p)'], ['2160', '4K']];
    const REASON_OPTIONS = [
      ['', 'All reasons'],
      ['vr_2d', '2D file → real VR'],
      ['vr_mono', 'mono VR → stereo VR'],
      ['vr_low_quality', 'VR below 4K → higher resolution'],
      ['below_cutoff', 'Quality below cutoff (all resolutions)'],
      ...BUCKETS.map(([k, t]) => [`below_cutoff:${k}`, `Quality below cutoff: ${t}`]),
      ['readd', 're-added by you'],
      ['duration', 'wrong length'],
    ];
    // State: open = still to do (not searched, searched, possible match); the others are what happened to a candidate.
    const STATE_OPTIONS = [
      ['open', 'Open'], ['check', 'To check'], ['downloading', 'Downloading'], ['wishlist', 'On the wishlist'], ['dismissed', 'Dismissed'], ['done', 'Done (replaced)'], ['all', 'All'],
    ];
    const stateOf = (c) => (c.status === 'wanted' || c.status === 'review' ? 'open' : c.status === 'grabbed' ? 'downloading' : c.status); // 'check' = found by the library check, waiting for a look
    const reasonSelect = el('select', { title: 'Why the file is on the list' }, REASON_OPTIONS.map(([v, t]) => el('option', { value: v, textContent: t })));
    const stateSelect = el('select', { title: 'Where the candidate stands' }, STATE_OPTIONS.map(([v, t]) => el('option', { value: v, textContent: t })));
    const unidentifiedKey = 'srw:upgrade-include-unidentified';
    let includeUnidentified = false;
    try { includeUnidentified = window.localStorage.getItem(unidentifiedKey) === '1'; } catch (e) { /* storage blocked */ }
    const unidentifiedBox = el('input', { type: 'checkbox', onChange: (e) => {
      includeUnidentified = e.target.checked;
      try { window.localStorage.setItem(unidentifiedKey, includeUnidentified ? '1' : '0'); } catch (err) { /* storage blocked */ }
      refreshCounts(); if (view) view.reset();
    } });
    unidentifiedBox.checked = includeUnidentified;
    const hiddenLine = el('span', { className: 'srw-hint' });
    // "Open" is everything still undecided -- the "To check" ones (found by a check, waiting for a look) are part of it (v0.241.1); "To check" narrows it.
    const matchesState = (c, want) => want === 'all' || stateOf(c) === want || (want === 'open' && stateOf(c) === 'check');
    const matchesReason = (c, want) => {
      if (!want) return true;
      if (want.startsWith('below_cutoff:')) return c.reason === 'below_cutoff' && resBucket(c) === want.slice(13);
      return c.reason === want;
    };
    const passesId = (c) => includeUnidentified || Boolean(c.provider_scene_id);
    // "Wrong length" (v0.241.0, moved here from the Duration mismatch page): shorter / longer than the provider says, with the threshold below.
    let durDirection = '';
    const passesDirection = (c) => !(reasonSelect.value === 'duration' && durDirection)
      || (c.reason === 'duration' && c.cur_duration && c.provider_duration && (durDirection === 'shorter' ? c.cur_duration < c.provider_duration : c.cur_duration > c.provider_duration));
    const visible = (c) => passesId(c) && passesDirection(c) && matchesState(c, stateSelect.value) && matchesReason(c, reasonSelect.value);
    // The counts behind each choice (the other choices and the id filter applied) and how many rows the id filter hides.
    function refreshCounts() {
      Array.from(reasonSelect.options).forEach((o, i) => {
        const n = candidates.filter((c) => passesId(c) && matchesState(c, stateSelect.value) && matchesReason(c, o.value)).length;
        o.textContent = `${REASON_OPTIONS[i][1]} (${n})`;
      });
      Array.from(stateSelect.options).forEach((o, i) => {
        const n = candidates.filter((c) => passesId(c) && matchesReason(c, reasonSelect.value) && matchesState(c, o.value)).length;
        o.textContent = `${STATE_OPTIONS[i][1]} (${n})`;
      });
      const hidden = candidates.filter((c) => stateOf(c) === 'open' && !passesId(c)).length;
      hiddenLine.textContent = !includeUnidentified && hidden ? `${hidden} scene(s) without a StashDB / ThePornDB id hidden.` : '';
    }
    // ---- sort ----
    const SORTS = [
      ['title', 'Title', (c) => (c.scene_title || '').toLowerCase(), true],
      ['size', 'File size', (c) => c.cur_size || 0, false],
      ['resolution', 'Resolution', (c) => (c.cur_height || 0), true],
      ['bitrate', 'Bitrate', (c) => c.cur_mbps || 0, true],
      ['searched', 'Last searched', (c) => c.last_search_at || '', true],
      ['date', 'Scene date', (c) => c.scene_date || '', false],
      ['reason', 'Reason', (c) => c.reason || '', true],
    ];
    let sortKey = 'title';
    let sortReversed = false;
    const sortSelect = el('select', { title: 'Sort the list' }, SORTS.map(([k, t]) => el('option', { value: k, textContent: `Sort: ${t}` })));
    const sortDir = el('button', { type: 'button', className: 'srw-btn', title: 'Reverse the order', textContent: '↑',
      onClick: () => { sortReversed = !sortReversed; sortDir.textContent = sortReversed ? '↓' : '↑'; view.reset(); } });
    sortSelect.addEventListener('change', () => { sortKey = sortSelect.value; view.reset(); });
    const sorted = () => {
      const def = SORTS.find((s) => s[0] === sortKey);
      const asc = def[3] !== sortReversed; // each sort starts in its natural direction (largest first for sizes, A-Z for titles)
      return candidates.filter(visible).sort((a, b) => {
        const x = def[2](a); const y = def[2](b);
        const d = typeof x === 'string' ? x.localeCompare(y) : x - y;
        return asc ? d : -d;
      });
    };
    // ---- selection and bulk actions ----
    const selected = new Set();
    const selCount = el('span', { className: 'srw-bulk-count' });
    const bulk = el('div', { className: 'srw-bulk-bar', hidden: true });
    const pageBox = el('input', { type: 'checkbox', title: 'Select / unselect everything on this page' });
    let pageIds = [];
    pageBox.addEventListener('change', () => { pageIds.forEach((id) => (pageBox.checked ? selected.add(id) : selected.delete(id))); syncSelection(); view.reload(); });
    const allLink = el('a', { href: '#', className: 'srw-bulk-all', onClick: (e) => { e.preventDefault(); sorted().forEach((c) => selected.add(c.id)); syncSelection(); view.reload(); } });
    const profileSelect = el('select', { title: 'Quality profile for the selected candidates (non-VR files; VR upgrades follow the VR rules)' });
    function syncSelection() {
      selCount.textContent = `${selected.size} selected`;
      const matching = sorted();
      allLink.textContent = selected.size < matching.length ? `Select all ${matching.length} matching` : '';
      bulk.hidden = selected.size === 0;
      pageBox.checked = pageIds.length > 0 && pageIds.every((id) => selected.has(id));
      searchAllBtn.disabled = selected.size > BULK_SEARCH_MAX;
      searchAllBtn.title = selected.size > BULK_SEARCH_MAX ? `At most ${BULK_SEARCH_MAX} searches at a time (each asks the indexers)` : 'Search the selected candidates now, one after the other';
    }
    const BULK_SEARCH_MAX = 10;
    const selectedCands = () => candidates.filter((c) => selected.has(c.id));
    async function runBulk(action, extra = {}) {
      const ids = [...selected];
      const r = await window.SRW.runOp('upgrades_bulk', { ids, action, ...extra });
      if (r.error) { notify('Upgrade wanted', r.error, 'error'); return null; }
      return r;
    }
    const bulkBtn = (label, title, onClick, className = 'srw-btn') => el('button', { type: 'button', className, textContent: label, title, onClick });
    const dismissAllBtn = bulkBtn('Dismiss', 'Keep the current files, do not search upgrades for the selected candidates', (e) => confirmInline(e.currentTarget,
      `Dismiss ${selected.size} candidate(s)? Their current files are kept.`, async () => {
        const ids = [...selected];
        const r = await runBulk('dismiss');
        if (!r) return;
        selected.clear(); await load();
        window.SRW.dismissedNotice(`Dismissed: ${r.changed} candidate(s)`, 'The current files are kept, no upgrade search.',
          () => window.SRW.runOp('upgrades_bulk', { ids, action: 'restore', statuses: r.previous }), () => load());
      }), 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss');
    const approveAllBtn = bulkBtn('Queue for search', 'Release the selected candidates that wait "To check" for the automatic upgrade search', async () => {
      const r = await runBulk('approve');
      if (!r) return;
      notify('Upgrade wanted', `${r.changed} candidate(s) queued for the upgrade search.`, 'success'); selected.clear(); load();
    });
    const restoreAllBtn = bulkBtn('Restore', 'Put the selected dismissed candidates back on the list', async () => {
      const r = await runBulk('restore');
      if (!r) return;
      notify('Upgrade wanted', `${r.restored} candidate(s) restored.`, 'success'); selected.clear(); load();
    });
    const wishlistAllBtn = bulkBtn('To wishlist', 'Put the selected candidates on the normal wishlist: its search takes only a release that is better than the current file (never a worse one, no last-resort fallback) and the old file is replaced after a verified import',
      (e) => confirmInline(e.currentTarget, `Put ${selected.size} candidate(s) on the wishlist? Its search replaces a file only with a better release.`, async () => {
        const ids = [...selected];
        const r = await runBulk('wishlist');
        if (!r) return;
        const failed = Object.keys(r.errors || {}).length;
        selected.clear(); await load();
        window.SRW.dismissedNotice(`On the wishlist: ${r.changed} candidate(s)`, failed ? `${failed} could not be moved (${Object.values(r.errors)[0]}).` : 'See Releases > On Wishlist > Only upgrades.',
          () => window.SRW.runOp('upgrades_bulk', { ids: Object.keys(r.previous).map(Number), action: 'unwishlist', previous: r.previous }), () => load());
      }));
    const searchAllBtn = bulkBtn('Search now', '', (e) => confirmInline(e.currentTarget,
      `Search upgrades for ${selected.size} candidate(s) now? Each is a request to your indexers; a better file is downloaded and replaces the current one after the verified import.`, async () => {
        window.SRW.setBusy(status, `searching ${selected.size}…`);
        const r = await runBulk('search');
        status.textContent = '';
        if (!r) return;
        notify('Upgrade search', `${r.grabbed} of ${r.results.length} grabbed; ${r.results.filter((x) => x.error).length} failed.`, r.grabbed ? 'success' : 'info');
        selected.clear(); load();
      }), 'srw-btn srw-btn-primary');
    const applyProfileBtn = bulkBtn('Apply', 'Set this quality profile on the selected candidates', async () => {
      const r = await runBulk('profile', { profile_id: Number(profileSelect.value) || null });
      if (!r) return;
      notify('Upgrade wanted', `Quality profile set on ${r.changed} candidate(s).`, 'success'); load();
    });
    const clearBtn = bulkBtn('Clear selection', '', () => { selected.clear(); syncSelection(); view.reload(); });
    bulk.append(el('span', { className: 'srw-bulk-head' }, [selCount, allLink]),
      el('span', { className: 'srw-bulk-actions' }, [searchAllBtn, wishlistAllBtn, approveAllBtn, dismissAllBtn, restoreAllBtn,
        el('span', { className: 'srw-bulk-profile' }, [el('span', { textContent: 'Quality profile' }), profileSelect, applyProfileBtn]), clearBtn]));
    // ---- the rest of the toolbar ----
    const autoLine = el('span', { className: 'srw-hint' });
    const refreshBtn = el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Find candidates',
      title: 'Rebuild the list from the VR check results and the library files',
      onClick: async () => {
        refreshBtn.disabled = true;
        status.textContent = ' collecting…';
        const r = await window.SRW.runOp('upgrades_refresh');
        refreshBtn.disabled = false;
        status.textContent = r.error ? ` error: ${r.error}`
          : ` found: ${r.vr_2d} 2D-in-VR, ${r.vr_mono} VR mono, ${r.vr_low_quality} low-quality VR, ${r.below_cutoff} below cutoff`;
        load();
      },
    });
    // One-time check of the whole library (v0.237.2): wrong lengths, 2D / mono / small VR files, files below the profile's cutoff.
    const checkLine = el('span', { className: 'srw-hint' });
    let polling = false;
    async function pollLibraryCheck() {
      if (polling) return;
      polling = true;
      try {
        for (;;) {
          const r = await window.SRW.runOp('upgrades_library_check_status');
          const j = (r && r.job) || {};
          if (r.error) { checkLine.textContent = `error: ${r.error}`; break; }
          if (j.running) {
            window.SRW.setBusy(checkLine, `${j.phase || 'working'}${j.total ? ` ${j.done || 0} / ${j.total}` : ''}`);
            await new Promise((res) => setTimeout(res, 3000));
            continue;
          }
          const lastRun = j.phase === 'done' ? j : (r.last || null); // this session's run, or the one kept from before
          const describe = (x) => {
            const c = x.counts || {};
            return `Library checked ${x.finished_at || ''} UTC: ${x.added_duration || 0} wrong-length file(s) added${x.skipped_duration ? ` (${x.skipped_duration} skipped: gone from Stash)` : ''}; found ${c.vr_2d || 0} 2D-in-VR, ${c.vr_mono || 0} VR mono, ${c.vr_low_quality || 0} low-bitrate VR, ${c.below_cutoff || 0} below the cutoff${x.cutoff ? ` (${x.cutoff}p)` : ' (no cutoff set in the quality profile)'}. The result stays in the list below; run it again only when you want a fresh look.`;
          };
          if (j.phase === 'failed') checkLine.textContent = `The library check failed: ${j.error}`;
          else if (lastRun && lastRun.finished_at) { checkLine.textContent = describe(lastRun); if (j.phase === 'done') await load(); }
          else checkLine.textContent = '';
          break;
        }
      } finally { polling = false; }
    }
    // ---- wrong length: the threshold, shorter / longer, and the duration check itself ----
    const durStatus = el('span', { className: 'srw-key-status' });
    const durThreshold = el('input', { type: 'number', min: '1', max: '90', value: '5', className: 'srw-input-narrow', title: 'Deviation in percent from which a file counts as "wrong length"' });
    const durDirSelect = el('select', { title: 'Only files shorter / longer than the provider says' },
      [['', 'shorter or longer'], ['shorter', 'file shorter'], ['longer', 'file longer']].map(([v, t]) => el('option', { value: v, textContent: t })));
    durDirSelect.addEventListener('change', () => { durDirection = durDirSelect.value; refreshCounts(); if (view) view.reset(); });
    let durPolling = false;
    let durSeenFinish; // the run that was already over when the page opened is not loaded again
    async function pollDuration() {
      if (durPolling) return;
      durPolling = true;
      durRunBtn.disabled = true;
      try {
        for (;;) {
          const st = await window.SRW.runOp('upgrades_duration_status');
          if (st.error) { durStatus.textContent = ` error: ${st.error}`; break; }
          if (st.threshold != null && document.activeElement !== durThreshold) durThreshold.value = String(st.threshold);
          const j = st.job || {};
          const sync = st.sync || {};
          if (sync.running) {
            if (durSeenFinish === undefined) durSeenFinish = null;
            durStatus.textContent = j.running ? ` asking the providers: ${j.phase || ''} ${j.done || 0} / ${j.total || 0}…`
              : ` ${sync.phase || 'working'}${sync.total ? ` (${sync.done || 0} / ${sync.total})` : ''}…`;
            await new Promise((resolve) => setTimeout(resolve, 3000));
            continue;
          }
          if (sync.error) durStatus.textContent = ` error: ${sync.error}`;
          else if (sync.result) {
            durStatus.textContent = ` ${sync.result.added} added, ${sync.result.removed} dropped${sync.result.skipped ? `, ${sync.result.skipped} skipped` : ''}`
              + `${st.last_checked ? ` — last answer of a provider ${st.last_checked} UTC` : ''}`;
            if (durSeenFinish !== undefined && durSeenFinish !== sync.finished_at) await load();
          } else durStatus.textContent = st.last_checked ? ` last answer of a provider ${st.last_checked} UTC, ${st.checked} scenes compared` : ' not run yet';
          durSeenFinish = sync.finished_at || null;
          break;
        }
      } finally { durPolling = false; durRunBtn.disabled = false; }
    }
    const durRunBtn = window.SRW.iconButton('button', 'faStopwatch', 'Run duration check', {
      title: 'Run duration check: ask the providers (StashDB / ThePornDB) for the length of every scene without a recent answer and compare it with the file; the first run takes long, later runs only ask about new scenes',
      onClick: (e) => confirmInline(e.currentTarget, 'Run the duration check? The providers are asked for the length of every scene without a recent answer (the first run can take an hour or more); then the "wrong length" list is updated.', async () => {
        const r = await window.SRW.runOp('upgrades_duration_sync', { run_check: true, threshold: Number(durThreshold.value) || 5 });
        if (r.error) { notify('Duration check', r.error, 'error'); return; }
        pollDuration();
      }),
    });
    durThreshold.addEventListener('change', async () => {
      durThreshold.value = String(Math.max(1, Math.min(90, Number(durThreshold.value) || 5)));
      const r = await window.SRW.runOp('upgrades_duration_sync', { threshold: Number(durThreshold.value) });
      if (r.error) { notify('Wrong length', r.error, 'error'); return; }
      pollDuration();
    });
    // The check itself is in the toolbar always; the threshold and shorter / longer only mean something for the reason "wrong length".
    const durBox = el('div', { className: 'srw-toolbar', hidden: true }, [
      el('label', {}, ['wrong length: deviation over ', durThreshold, ' %']), durDirSelect,
    ]);
    reasonSelect.addEventListener('change', () => { durBox.hidden = reasonSelect.value !== 'duration'; refreshCounts(); if (view) view.reset(); });
    const libraryCheckBtn = el('button', {
      type: 'button', className: 'srw-btn', textContent: 'Check the whole library…',
      title: 'Look at every scene of your library once: wrong length, 2D / mono / low-bitrate VR files, files below the quality profile cutoff',
      onClick: (e) => confirmInline(e.currentTarget,
        'Check the whole library once? It asks the providers for the length of every scene (the first run can take an hour or more), looks for 2D / mono / low-bitrate VR files and for files below the cutoff of the quality profile. It only fills this list; nothing is downloaded. The result stays; a second run within 30 days reuses the providers\' lengths and is quick.',
        async () => {
          const r = await window.SRW.runOp('upgrades_library_check_start', {});
          if (r.error) { notify('Library check', r.error, 'error'); return; }
          if (r.skipped) notify('Library check', r.reason, 'info');
          pollLibraryCheck();
        }),
    });
    const section = el('details', { className: 'srw-settings-section srw-panel srw-upgrades' }, [
      el('summary', { textContent: 'Upgrade wanted' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Library files that should be replaced by a better version: 2D files of VR scenes → real VR, '
          + 'monoscopic VR → stereoscopic, VR below 4K / low bitrate → higher resolution (never above 6K / 15 GB), '
          + 'non-VR below the profile cutoff, and files with a wrong length (cut or partial rips: choose the reason "wrong length" for the threshold, shorter / longer and the duration check). Only candidates that are actually better are grabbed; after the '
          + 'verified import the old file goes to the recycle bin.',
      }),
      el('div', { className: 'srw-toolbar srw-upgrade-toolbar' }, [
        refreshBtn, libraryCheckBtn, durRunBtn, reasonSelect, stateSelect, sortSelect, sortDir,
        el('label', { title: 'Without an id the scene cannot be searched yet; identify it first (Maintenance > Unidentified scenes)' },
          [unidentifiedBox, ' include scenes without StashDB / ThePornDB id']), status,
      ]),
      durBox,
      el('p', { className: 'srw-hint' }, [durStatus]),
      el('p', { className: 'srw-hint' }, [checkLine]),
      el('p', { className: 'srw-hint' }, [hiddenLine]),
      el('p', { className: 'srw-hint' }, [autoLine, ' ', el('a', { href: '/plugins/stash-release-manager/settings#jobs', textContent: 'Change in Settings > Jobs' })]),
      bulk,
      list,
    ]);

    pollDuration(); // shows the threshold and the last check; a run that is still going is picked up again
    let candidates = [];
    let view = null;
    let dryRunButton = false;
    let profiles = [];
    let activeProfileId = '';
    const profileName = (id) => (profiles.find((p) => p.id === id) || {}).name;
    async function load() {
      const r = await window.SRW.runOp('upgrades_list');
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      candidates = r.candidates || [];
      dryRunButton = Boolean(r.dry_run_button);
      profiles = r.profiles || [];
      activeProfileId = r.active_profile_id || '';
      const keep = profileSelect.value;
      profileSelect.replaceChildren(el('option', { value: '0', textContent: `Settings profile${profileName(Number(activeProfileId)) ? ` (${profileName(Number(activeProfileId))})` : ''}` }),
        ...profiles.map((p) => el('option', { value: String(p.id), textContent: p.name })));
      profileSelect.value = keep || '0';
      for (const id of [...selected]) if (!candidates.some((c) => c.id === id)) selected.delete(id);
      refreshCounts();
      autoLine.textContent = r.enabled
        ? `Automatic search: on, a few candidates (${r.per_run || '20'} per run) at most every ${r.interval_hours || '12'} h.`
        : 'Automatic search: off.';
      if (!view) {
        view = window.SRW.buildFilteredPagedList({
          filters: ['search'],
          perPage: 30,
          showFilterBarFrom: 10,
          emptyText: 'No upgrade candidates here (try another state, or click "Find candidates").',
          fetchPage: window.SRW.localPageFetcher(() => sorted(), { text: (c) => `${c.scene_title} ${c.file_path} ${c.studio_name || ''}` }),
          renderItems: (items) => { pageIds = items.map((c) => c.id); syncSelection(); return el('ul', { className: 'srw-release-list srw-upgrade-list' }, [
            el('li', { className: 'srw-upgrade-selectall' }, [el('label', {}, [pageBox, ' select this page'])]), ...items.map(buildRow)]); },
        });
        list.appendChild(view.node);
        reasonSelect.addEventListener('change', () => { refreshCounts(); view.reset(); });
        stateSelect.addEventListener('change', () => { refreshCounts(); view.reset(); });
      }
      view.reload();
      syncSelection();
    }

    function fmtGb(bytes) { return bytes ? `${(bytes / 1e9).toFixed(1)} GB` : '?'; }
    const fmtMin = (s) => (s ? `${Math.round(s / 60)} min` : null);

    function showDryRun(c, r) {
      const counts = {};
      (r.candidates || []).forEach((x) => { counts[x.verdict] = (counts[x.verdict] || 0) + 1; });
      const body = el('div', { className: 'srw-manual-search-panel' }, [
        el('p', { className: r.would_grab ? 'srw-review-warning' : 'srw-hint', textContent: r.would_grab
          ? `Would grab: ${r.would_grab.title} (${fmtGb(r.would_grab.size)}, ${r.would_grab.indexer})`
          : `Nothing better found (${r.result}).` }),
        el('p', { className: 'srw-hint', textContent: Object.entries(counts).map(([k, n]) => `${n}× ${k}`).join(', ') }),
        el('div', { className: 'srw-result-list' }, (r.candidates || []).slice(0, 60).map((x) => el('div', { className: 'srw-manual-search-result srw-result-row' }, [
          el('div', { className: 'srw-result-main' }, [
            el('div', { className: 'srw-result-head' }, [el('span', { className: 'srw-verdict', textContent: x.verdict }), el('span', { textContent: x.title })]),
            el('div', { className: 'srw-result-facts' }, [el('span', { className: 'srw-chips' }, [x.indexer, fmtGb(x.size)].filter(Boolean).map((t) => el('span', { className: 'srw-chip', textContent: t })))]),
          ]),
        ]))),
      ]);
      window.SRW.openModal('Dry run (nothing is downloaded)', body, { subtitle: window.SRW.sceneSubtitle({ title: c.scene_title, studio_name: c.studio_name, scene_date: c.scene_date }) });
    }

    function buildRow(c) {
      const [badge, why] = UPGRADE_REASON[c.reason] || [c.reason, ''];
      const durDir = c.reason === 'duration' && c.cur_duration && c.provider_duration ? (c.cur_duration < c.provider_duration ? 'shorter' : 'longer') : null;
      const sceneHref = `/scenes/${c.scene_id}`;
      const result = el('div', { className: 'srw-hint srw-upgrade-note' });
      const statusText = {
        wanted: c.last_search_at ? `last searched ${c.last_search_at.slice(0, 16)}: ${c.last_search_result || '—'}` : 'not searched yet',
        grabbed: 'upgrade downloading / importing',
        review: 'possible better file found -- see Activity > Needs review',
        done: 'replaced (old file in the recycle bin)',
        check: 'found by the library check -- file and provider disagree on the length; look at it, then queue it for the upgrade search or dismiss it',
        dismissed: 'dismissed -- the current file is kept',
        wishlist: 'on the wishlist -- the wishlist search takes care of it',
      }[c.status] || c.status;
      const actions = [];
      const cb = el('input', { type: 'checkbox', title: 'Select' });
      cb.checked = selected.has(c.id);
      cb.addEventListener('change', () => { if (cb.checked) selected.add(c.id); else selected.delete(c.id); syncSelection(); });
      if (c.status === 'wanted' || c.status === 'review' || c.status === 'check') {
        const grabBtn = window.SRW.iconButton('button', 'faMagnifyingGlass', 'Search now', {
          title: 'Search now: look for an upgrade and download it; after the verified import the current file goes to the recycle bin',
          onClick: (e) => confirmInline(e.currentTarget,
            `Search an upgrade for "${c.scene_title}" and download it? After the verified import the current file goes to the recycle bin.`, async () => {
              window.SRW.setBusy(result, 'searching…');
              const r = await window.SRW.runOp('upgrades_search', { id: c.id, dry_run: false });
              result.textContent = '';
              if (r.error) { notify('Upgrade', r.error, 'error'); return; }
              notify('Upgrade', r.grab ? `Grabbed: ${r.grab.title}` : `Nothing better found (${r.result}).`, r.grab ? 'success' : 'info');
              load();
            }),
        });
        const wishBtn = window.SRW.iconButton('button', 'faHeart', 'To wishlist', {
          title: 'To wishlist: the normal wishlist search takes over -- only a release that is better than the current file is taken, never a worse one; the old file is replaced after a verified import',
          onClick: (e) => confirmInline(e.currentTarget, `Put "${c.scene_title}" on the wishlist? Its search replaces the file only with a better release.`, async () => {
            const r = await window.SRW.runOp('upgrades_bulk', { ids: [c.id], action: 'wishlist' });
            if (r.error) { notify('To wishlist', r.error, 'error'); return; }
            if (r.errors && r.errors[String(c.id)]) { notify('To wishlist', r.errors[String(c.id)], 'error'); return; }
            await load();
            window.SRW.dismissedNotice(`On the wishlist: ${c.scene_title || 'upgrade'}`, 'See Releases > On Wishlist > Only upgrades.',
              () => window.SRW.runOp('upgrades_bulk', { ids: [c.id], action: 'unwishlist', previous: r.previous }), () => load());
          }),
        });
        const dismissBtn = el('button', {
          className: 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss', textContent: 'Dismiss', title: 'Keep the current file, don\'t search an upgrade',
          onClick: async () => {
            const res = await window.SRW.runOp('upgrades_dismiss', { id: c.id });
            if (res && res.error) { notify('Dismiss', `Dismiss failed: ${res.error}`, 'error'); return; }
            await load();
            window.SRW.dismissedNotice(`Dismissed: ${c.scene_title || 'upgrade'}`, 'The current file is kept, no upgrade search.',
              () => window.SRW.runOp('upgrades_restore', { id: c.id, status: res.previous || 'wanted' }), () => load());
          },
        });
        // Own query: type a search, pick a result, Grab -- in a modal. The grab is recorded as an upgrade of THIS file.
        const searchSlot = el('span', { className: 'srw-search-actions' });
        window.SRW.getSearchCapabilities().then((caps) => {
          const sources = (caps && caps.manual_sources) || [];
          if (!sources.length) return;
          const asRelease = { provider: c.provider, scene_stashdb_id: c.provider_scene_id, title: c.scene_title, studio_name: c.studio_name, scene_date: c.scene_date };
          searchSlot.appendChild(window.SRW.buildManualSearch(asRelease, sources, caps.backend, () => load(), {
            grab: (candidate) => window.SRW.runOp('upgrades_manual_grab', { id: c.id, candidate }),
          }));
        });
        if (c.provider_scene_id) {
          if (dryRunButton) {
            actions.push(el('button', {
              type: 'button', className: 'srw-btn', textContent: 'Dry run', title: 'Search and show what would be grabbed -- nothing is downloaded',
              onClick: async (e) => {
                const b = e.currentTarget; b.disabled = true; window.SRW.setBusy(result, 'searching…');
                const r = await window.SRW.runOp('upgrades_search', { id: c.id, dry_run: true });
                b.disabled = false; result.textContent = '';
                if (r.error) { notify('Dry run', r.error, 'error'); return; }
                showDryRun(c, r);
              },
            }));
          }
          if (c.status === 'check') {
            actions.push(window.SRW.iconButton('button', 'faCheck', 'Queue for search', {
              title: 'Queue for search: the length looks right to you -- the automatic upgrade search may take this candidate',
              onClick: async () => {
                const r = await window.SRW.runOp('upgrades_bulk', { ids: [c.id], action: 'approve' });
                if (r.error) { notify('Queue for search', r.error, 'error'); return; }
                load();
              },
            }));
          }
          actions.push(grabBtn, searchSlot, wishBtn, dismissBtn);
        } else {
          actions.push(
            el('span', { className: 'srw-hint', textContent: 'No StashDB / ThePornDB id yet -- identify the scene first.' }),
            // The scene goes straight to Review: Identify Scene (v0.241.5; the link to Unidentified scenes only opened that page, and a scene that is
            // parked in _UNMATCHED or already tagged is not listed there).
            window.SRW.iconButton('button', ['faFingerprint', 'faMagnifyingGlass'], 'Send to Identify Scene', {
              title: 'Send to Identify Scene: adds the tag "Review: Identify Scene" (nothing else changes); the tools to find out what the scene is are there (Maintenance > Review: Identify Scene)',
              onClick: async (e) => {
                const b = e.currentTarget;
                b.disabled = true;
                const r = await window.SRW.runOp('compare_review_add', { scene_id: c.scene_id });
                b.disabled = false;
                result.textContent = r.error ? ` error: ${r.error}` : (r.already ? ' is in Review: Identify Scene already' : ' sent to Review: Identify Scene');
              },
            }),
            dismissBtn);
        }
      } else if (c.status === 'dismissed') {
        actions.push(window.SRW.iconButton('button', 'faRotateLeft', 'Restore', { className: 'srw-btn', title: 'Restore: back on the list',
          onClick: async () => {
            const r = await window.SRW.runOp('upgrades_bulk', { ids: [c.id], action: 'restore' });
            if (r.error) { notify('Restore', r.error, 'error'); return; }
            load();
          } }));
      } else if (c.status === 'wishlist') {
        actions.push(el('a', { className: 'srw-btn', href: '/plugins/stash-release-manager', textContent: 'On Wishlist', title: 'Releases > On Wishlist (filter: Only upgrades)' }));
      }
      const chips = [`${c.cur_width}×${c.cur_height}`, fmtGb(c.cur_size), c.cur_mbps ? `${Math.round(c.cur_mbps)} Mbit/s` : null,
        c.reason === 'duration' && c.cur_duration && c.provider_duration
          ? `file ${fmtMin(c.cur_duration)} · provider ${fmtMin(c.provider_duration)} (${c.cur_duration < c.provider_duration ? 'shorter' : 'longer'} by ${Math.round(Math.abs(c.cur_duration - c.provider_duration) / c.provider_duration * 100)} %)` : fmtMin(c.cur_duration),
        c.quality_profile_id && profileName(c.quality_profile_id) ? `profile: ${profileName(c.quality_profile_id)}` : null,
        c.studio_name || null, c.scene_date || null].filter(Boolean);
      return el('li', { className: 'srw-release-item srw-upgrade-row' }, [
        cb,
        el('a', { className: 'srw-vr-thumb-link', href: sceneHref, target: '_blank', rel: 'noopener noreferrer' },
          [el('img', { className: 'srw-vr-thumb', src: `/scene/${c.scene_id}/screenshot`, loading: 'lazy', alt: '' })]),
        el('div', { className: 'srw-upgrade-info' }, [
          el('div', { className: 'srw-result-head' }, [
            // A wrong-length file shows its direction in the pill (v0.241.2: "file shorter" / "file longer", the colours of the old Duration page).
            el('span', { className: `srw-upgrade-reason srw-upgrade-reason-${durDir ? (durDir === 'shorter' ? 'dur_short' : 'dur_long') : c.reason}`,
              textContent: durDir ? `file ${durDir}` : badge,
              title: durDir ? (durDir === 'shorter' ? 'The file is shorter than the provider\'s length: a cut or partial rip, a sample, or a wrong length at the provider'
                : 'The file is longer than the provider\'s length (a wrong length at the provider, or a film in one file)') : why }),
            el('a', { className: 'srw-release-title', href: sceneHref, target: '_blank', rel: 'noopener noreferrer', textContent: c.scene_title || `Scene ${c.scene_id}` }),
          ]),
          el('div', { className: 'srw-result-facts' }, [el('span', { className: 'srw-chips' }, chips.map((t) => el('span', { className: 'srw-chip', textContent: t })))]),
          el('div', { className: 'srw-hint', textContent: statusText }),
          el('div', { className: 'srw-maintenance-file-path', textContent: c.file_path }),
          result,
        ]),
        el('div', { className: 'srw-upgrade-actions' }, actions),
      ]);
    }

    load();
    pollLibraryCheck(); // shows a check that is running already (or the result of the last one)
    return window.SRW.rememberOpen(section, 'maintenance:upgrade-wanted', false);
  }


  // ==================== Compare scenes (2026-09-30, api/scene_compare.py) ====================
  //
  // Two Stash scenes side by side, both playable, to decide visually which one carries the right provider id
  // (or which file to keep). Pairs come from the Organize name-conflict list, scripts or "Add pair" below.
  // The only write is "Remove this ID" (backed up to compare_removed_ids.jsonl + History).
  function buildCompareSection(part = 'both') {
    const { el, confirmInline } = window.SRW;
    const status = el('span', { className: 'srw-key-status' });
    const list = el('div');
    const viewer = el('div', { className: 'srw-cmp-viewer' });
    // Filter buttons (2026-10-02): Open + Later is the default so a "later" pair never disappears; the counts show what is hidden.
    const FILTERS = [['active', 'Open + Later'], ['pending', 'Open'], ['skipped', 'Later'], ['done', 'Done'], ['', 'All']];
    let statusFilter = 'active';
    const filterBtns = FILTERS.map(([value, label]) => {
      const b = el('button', { className: 'srw-btn srw-btn-sm', textContent: label });
      b.addEventListener('click', () => { statusFilter = value; paintFilters(); applyFilter(); });
      return { value, label, b };
    });
    const paintFilters = () => filterBtns.forEach((f) => f.b.classList.toggle('srw-btn-primary', f.value === statusFilter));
    const statusBar = el('span', {}, filterBtns.map((f) => f.b));
    const aIn = el('input', { type: 'text', placeholder: 'Scene A id', style: 'width: 6rem' });
    const bIn = el('input', { type: 'text', placeholder: 'Scene B id', style: 'width: 6rem' });
    const labelIn = el('input', { type: 'text', placeholder: 'Label (optional)', style: 'width: 12rem' });
    const addBtn = el('button', {
      className: 'srw-btn', textContent: 'Add pair',
      onClick: async () => {
        if (!aIn.value.trim() || !bIn.value.trim()) { window.SRW.notify('Compare scenes', 'Enter both scene ids.', 'warning'); return; }
        const r = await window.SRW.runOp('compare_add', { a: aIn.value.trim(), b: bIn.value.trim(), label: labelIn.value.trim() });
        if (r.error) { window.SRW.notify('Compare scenes', r.error, 'error'); return; }
        window.SRW.notify('Compare scenes', r.added ? 'Pair added.' : 'That pair already exists.', r.added ? 'success' : 'info');
        aIn.value = ''; bIn.value = ''; labelIn.value = '';
        load();
      },
    });
    const refreshBtn = window.SRW.iconButton('button', 'faRotate', 'Reload', { className: 'srw-btn', title: 'Reload: ask again, the list shown is a kept one', onClick: () => load() });
    const closeBtn = el('button', {
      className: 'srw-btn', textContent: 'Close resolved pairs',
      title: 'Open pairs where one scene is gone, or both scenes have different provider ids and neither is marked for review',
      onClick: async () => {
        const dry = await window.SRW.runOp('compare_close_resolved', { dry_run: true });
        if (dry.error) { window.SRW.notify('Compare scenes', dry.error, 'error'); return; }
        if (!(dry.pairs || []).length) { window.SRW.notify('Compare scenes', 'No resolved open pairs.', 'info'); return; }
        window.SRW.confirmInline(closeBtn, `Close ${dry.pairs.length} resolved pair(s)? ${dry.pairs.slice(0, 4).map((x) => x.label.slice(0, 40)).join(' | ')}${dry.pairs.length > 4 ? ' …' : ''}`, async () => {
          const r = await window.SRW.runOp('compare_close_resolved', { dry_run: false });
          if (r.error) { window.SRW.notify('Compare scenes', r.error, 'error'); return; }
          window.SRW.notify('Compare scenes', `${r.closed} pair(s) closed.`, 'success');
          load();
        });
      },
    });
    const aliasPanel = buildAliasPanel();
    const section = el('details', { className: 'srw-settings-section srw-panel srw-compare' }, [
      el('summary', { textContent: 'Compare scenes' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Two scenes side by side -- play both, compare file and metadata and what each provider id really '
          + 'points to, then remove the wrong ID or mark the pair done. Nothing is removed without a confirmation; the '
          + 'scene is saved to compare_removed_ids.jsonl and a History entry is written first.',
      }),
      el('div', { className: 'srw-toolbar' }, [statusBar, refreshBtn, closeBtn, status]),
      el('div', { className: 'srw-toolbar' }, [aIn, bIn, labelIn, addBtn]),
      list,
      viewer,
      aliasPanel,
    ]);

    let pairs = [];
    let view = null;
    let openId = null;

    let allPairs = [];
    const applyFilter = () => {
      pairs = allPairs.filter((p) => (statusFilter === 'active' ? p.status !== 'done' : !statusFilter || p.status === statusFilter));
      const n = (st) => allPairs.filter((p) => p.status === st).length;
      const counts = { active: n('pending') + n('skipped'), pending: n('pending'), skipped: n('skipped'), done: n('done'), '': allPairs.length };
      filterBtns.forEach((f) => { f.b.textContent = `${f.label} (${counts[f.value]})`; });
      status.textContent = ` ${pairs.length} pair(s)`;
      if (view) view.reload();
    };

    async function load() {
      status.textContent = ' loading…';
      const r = await window.SRW.runOp('compare_pairs', { status: '' });
      if (r.error) { status.textContent = ` error: ${r.error}`; return; }
      allPairs = r.pairs || [];
      if (!view) {
        view = window.SRW.buildFilteredPagedList({
          filters: ['search'], perPage: 20, showFilterBarFrom: 10,
          emptyText: 'No pairs here. Add one above (scene ids from the Stash scene URLs).',
          fetchPage: window.SRW.localPageFetcher(() => pairs, {
            text: (p) => `${p.label || ''} ${p.a ? p.a.title : ''} ${p.b ? p.b.title : ''} ${p.a ? p.a.studio : ''} ${p.scene_a} ${p.scene_b}`,
          }),
          renderItems: (items) => el('ul', { className: 'srw-release-list' }, items.map(buildPairRow)),
        });
        list.appendChild(view.node);
      }
      paintFilters();
      applyFilter();
    }

    function sceneSummary(tag, s, id) {
      if (!s) return `${tag}: #${id} (not in Stash)`;
      const res = s.height ? `${s.height}p` : '?';
      return `${tag}: #${s.id} ${s.title || '(no title)'} — ${res}, ${s.minutes} min, ${s.mb} MB`;
    }

    function buildPairRow(p) {
      const badge = { pending: 'open', skipped: 'later', done: 'done' }[p.status] || p.status;
      return el('li', { className: 'srw-release-item' }, [
        el('span', { className: `srw-upgrade-reason srw-upgrade-reason-${p.status === 'done' ? 'done' : 'vr_2d'}`, textContent: badge }),
        el('div', {}, [
          el('div', { className: 'srw-release-title', textContent: p.label || (p.a && p.a.title) || `Pair ${p.id}` }),
          el('div', { className: 'srw-hint', textContent: sceneSummary('A', p.a, p.scene_a) }),
          el('div', { className: 'srw-hint', textContent: sceneSummary('B', p.b, p.scene_b) }),
          p.note ? el('div', { className: 'srw-hint', textContent: `note: ${p.note}` }) : null,
        ]),
        el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Compare', onClick: () => openPair(p.id) }),
      ]);
    }

    // After a change: pairs reload both scenes, the review view (p.refresh) reloads its single scene.
    const reopen = (p) => (p.refresh ? p.refresh() : openPair(p.id));
    const fmtDur = (s) => { if (!s) return '?'; const m = Math.floor(s / 60); return `${m}:${String(Math.round(s % 60)).padStart(2, '0')} min`; };
    const fmtMb = (b) => (b ? (b >= 1e9 ? `${(b / 1e9).toFixed(2)} GB` : `${Math.round(b / 1e6)} MB`) : '?');

    async function openPair(id) {
      openId = id;
      viewer.replaceChildren(el('p', { className: 'srw-hint', textContent: 'Loading both scenes and the provider entries…' }));
      viewer.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const p = await window.SRW.runOp('compare_pair', { id });
      if (openId !== id) return;
      if (p.error) { viewer.replaceChildren(el('p', { className: 'srw-hint', textContent: `error: ${p.error}` })); return; }
      renderPair(p);
    }

    function renderPair(p) {
      const videos = [];
      const cols = ['a', 'b'].map((side) => buildColumn(p, side, p[side], videos));
      const dist = p.phash_distance;
      const distText = dist == null ? '' : dist <= 8
        ? ` Both files look like the SAME video (phash distance ${dist}).`
        : ` The files are DIFFERENT videos (phash distance ${dist}).`;
      const sharedText = p.shared_ids && p.shared_ids.length
        ? `Both scenes carry the same provider id (${p.shared_ids.map((x) => x.slice(0, 8)).join(', ')}).` : 'The scenes carry different provider ids.';
      const audio = el('select', { title: 'Which scene plays sound' }, [
        el('option', { value: 'a', textContent: 'Sound: A' }), el('option', { value: 'b', textContent: 'Sound: B' }),
        el('option', { value: '', textContent: 'Sound: off' }),
      ]);
      const applyAudio = () => videos.forEach((pl, i) => { pl.video.muted = audio.value !== ['a', 'b'][i]; });
      audio.addEventListener('change', applyAudio);
      applyAudio();
      const each = (fn) => videos.forEach((pl) => { try { fn(pl); } catch (e) { /* not ready yet */ } });
      const playAll = () => each((pl) => { const pr = pl.video.play(); if (pr && pr.catch) pr.catch(() => { /* autoplay policy: needs a user gesture */ }); });
      const bar = el('div', { className: 'srw-toolbar srw-cmp-bar' }, [
        window.SRW.iconButton('button', 'faPlay', 'Play both', { className: 'srw-btn srw-btn-sm', onClick: playAll }),
        window.SRW.iconButton('button', 'faPause', 'Pause both', { className: 'srw-btn srw-btn-sm', onClick: () => each((pl) => pl.video.pause()) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: '−10 s', onClick: () => each((pl) => pl.seek(pl.time() - 10)) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: '+10 s', onClick: () => each((pl) => pl.seek(pl.time() + 10)) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: '+60 s', onClick: () => each((pl) => pl.seek(pl.time() + 60)) }),
        el('button', { className: 'srw-btn srw-btn-sm', title: 'Jump both to the same position (the time of A)',
          textContent: 'B to A’s time', onClick: () => { if (videos[1] && videos[0]) videos[1].seek(videos[0].time()); } }),
        el('button', { className: 'srw-btn srw-btn-sm', title: 'Both to the middle of their own length -- the quick way to compare different cuts',
          textContent: 'Jump to 50 %', onClick: () => each((pl) => pl.seek(pl.total * 0.5)) }),
        audio,
      ]);
      const setStatus = async (s) => {
        const r = await window.SRW.runOp('compare_status', { id: p.id, status: s });
        if (r.error) { window.SRW.notify('Compare scenes', r.error, 'error'); return; }
        window.SRW.notify('Compare scenes', s === 'done' ? 'Marked done.' : s === 'skipped' ? 'Put on "later".' : 'Reopened.', 'success');
        viewer.replaceChildren(); openId = null; load();
      };
      const footer = el('div', { className: 'srw-toolbar' }, [
        el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Done', onClick: () => setStatus('done') }),
        el('button', { className: 'srw-btn', textContent: 'Later', onClick: () => setStatus('skipped') }),
        el('button', { className: 'srw-btn', textContent: 'Reopen', onClick: () => setStatus('pending') }),
        el('button', { className: 'srw-btn', textContent: 'Close', onClick: () => { viewer.replaceChildren(); openId = null; } }),
      ]);
      viewer.replaceChildren(
        el('h4', { textContent: p.label || `Pair ${p.id}` }),
        el('p', { className: 'srw-hint', textContent: `${sharedText}${distText}` }),
        bar,
        el('div', { className: 'srw-cmp-grid' }, cols),
        footer,
      );
    }

    function buildColumn(p, side, s, videos) {
      const tag = side === 'review' ? 'Scene' : side.toUpperCase();
      if (!s || s.missing) return el('div', { className: 'srw-cmp-col' }, [el('h5', { textContent: `${tag}: scene not found` })]);
      const f = (s.files && s.files[0]) || {};
      // Containers a browser can not play natively go through Stash's transcode from the start; seeking in a
      // transcoded stream = reload from a start time (the native scrubber only knows the buffered part then).
      const ext = ((f.path || '').split('.').pop() || '').toLowerCase();
      const NATIVE_OK = ['mp4', 'm4v', 'webm', 'mov', 'mkv'];
      const total = f.duration || 0;
      // No poster: Stash's screenshot is the provider's cover image, not a frame of this file.
      const video = el('video', { className: 'srw-cmp-video', controls: 'controls', preload: 'metadata', playsinline: 'playsinline' });
      const pl = {
        video, total, transcoded: false, offset: 0,
        url(start) { return `/scene/${s.id}/stream.mp4?resolution=STANDARD${start ? `&start=${Math.floor(start)}` : ''}`; },
        time() { return this.offset + (video.currentTime || 0); },
        seek(to) {
          const tt = Math.max(0, Math.min(this.total || to, to));
          if (!this.transcoded) { video.currentTime = tt; return; }
          const wasPlaying = !video.paused;
          const wasMuted = video.muted;
          this.offset = tt; video.src = this.url(tt); video.muted = wasMuted; video.load();
          if (wasPlaying) { const pr = video.play(); if (pr && pr.catch) pr.catch(() => {}); }
        },
        useTranscode(msg) {
          this.transcoded = true; this.offset = 0; video.src = this.url(0); video.load();
          note.textContent = msg || 'Played through Stash’s transcode (lower quality). Use the buttons above to jump; the player’s own bar only covers what is loaded.';
        },
      };
      videos.push(pl);
      const note = el('div', { className: 'srw-hint' });
      if (NATIVE_OK.includes(ext)) {
        video.src = `/scene/${s.id}/stream`;
        video.addEventListener('error', () => {
          if (pl.transcoded) { note.textContent = 'Stash’s transcode failed too -- open the scene in Stash.'; return; }
          note.replaceChildren('The browser can not play this file directly. ',
            window.SRW.iconButton('button', 'faTv', 'Play through Stash’s transcode', { className: 'srw-btn srw-btn-sm', onClick: () => pl.useTranscode() }));
        });
      } else {
        pl.useTranscode(`.${ext || '?'} is not playable in a browser -- played through Stash’s transcode (lower quality, starts slower). `
          + 'Use the buttons above to jump; the player’s own bar only covers what is loaded.');
        video.addEventListener('error', () => { note.textContent = 'Stash’s transcode failed -- open the scene in Stash.'; });
      }
      const rows = [
        ['Title', s.title || '(none)'], ['Date', s.date || '—'], ['Studio', s.studio || '—'],
        ['Performers', (s.performers || []).join(', ') || '—'],
        ['Length', fmtDur(f.duration)], ['Resolution', f.width ? `${f.width}×${f.height}` : '?'],
        ['Bitrate', f.bit_rate ? `${(f.bit_rate / 1e6).toFixed(1)} Mbit/s` : '?'], ['Size', fmtMb(f.size)],
        ['Codec', f.codec ? `${f.codec}${f.fps ? `, ${Math.round(f.fps)} fps` : ''}` : '?'],
        ['File', (s.files || []).map((x) => x.path).join('\n') || '—'],
        ['Markers', (s.markers || []).length ? s.markers.map((m) => `${Math.round(m.seconds)}s ${m.tag || ''}`).join(', ') : '0'],
        ['Rating / plays', `${s.rating100 == null ? '—' : s.rating100} / ${s.plays || 0} plays, O ${s.o || 0}`],
        ['Tags', `${(s.tags || []).length}${(s.tags || []).length ? `: ${s.tags.slice(0, 8).join(', ')}${s.tags.length > 8 ? '…' : ''}` : ''}`],
        ['Group', (s.groups || []).join(', ') || '—'],
      ];
      const table = el('table', { className: 'srw-cmp-table' }, rows.map(([k, v]) =>
        el('tr', {}, [el('th', { textContent: k }), el('td', { className: k === 'File' ? 'srw-maintenance-file-path' : '', textContent: v })])));
      const ids = (s.stash_ids || []).length ? s.stash_ids.map((i) => {
        const ps = i.provider_scene;
        const info = !ps ? 'not found at the provider' : ps.error ? `lookup failed: ${ps.error}`
          : `“${ps.title || '?'}” · ${ps.date || '?'} · ${ps.studio || '?'} · ${(ps.performers || []).join(', ') || '—'}`;
        const rm = el('button', { className: 'srw-btn srw-btn-sm srw-btn-danger', textContent: `Remove this ID from ${tag}` });
        rm.addEventListener('click', (e) => confirmInline(e.currentTarget,
          `Remove ${i.provider || 'this'} id ${i.stash_id.slice(0, 8)}… from scene #${s.id}? The scene stays as it is; the id is saved to a backup file.`,
          async () => {
            const r = await window.SRW.runOp('compare_remove_id', { id: p.id, scene_id: s.id, stash_id: i.stash_id });
            if (r.error) { window.SRW.notify('Compare scenes', r.error, 'error'); return; }
            window.SRW.notify('Compare scenes', `ID removed from scene #${s.id}.`, 'success');
            reopen(p);
          }, { danger: true }));
        return el('div', { className: 'srw-cmp-id' }, [
          el('div', {}, [
            el('strong', { textContent: `${i.provider || i.endpoint} ` }),
            i.url ? el('a', { href: i.url, target: '_blank', rel: 'noopener noreferrer', textContent: `${i.stash_id.slice(0, 8)}…` }) : i.stash_id.slice(0, 8),
          ]),
          el('div', { className: 'srw-hint', textContent: info }),
          rm,
        ]);
      }) : [el('div', { className: 'srw-hint', textContent: 'No provider id on this scene.' })];
      const wrap = el('div', { className: 'srw-cmp-videowrap', style: 'position: relative' }, [video]);
      const heading = el('h5', {}, [`${tag}: `, el('a', { href: `/scenes/${s.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: `#${s.id} open in Stash` })]);
      if (side === 'review') {
        // The review has the whole width of its dialog (v0.240.7): the player and the facts left, the ids and the tools to identify the scene right.
        return el('div', { className: 'srw-cmp-col srw-cmp-split' }, [
          el('div', { className: 'srw-cmp-split-left' }, [heading, wrap, note, buildFaceHint(s), buildFramesStrip(s), table]),
          el('div', { className: 'srw-cmp-split-right' }, [
            el('div', { className: 'srw-cmp-ids' }, ids),
            (s.stash_ids || []).length ? null : buildIdentifyPanel(p, s, tag),
          ].filter(Boolean)),
        ]);
      }
      return el('div', { className: 'srw-cmp-col' }, [
        heading,
        wrap, note, buildFaceHint(s), buildFramesStrip(s), table,
        el('div', { className: 'srw-cmp-ids' }, ids),
        (s.stash_ids || []).length ? null : buildIdentifyPanel(p, s, tag),
      ]);
    }

    // Alias provider ids: a StashDB scene whose video is owned as ANOTHER local scene (the producer sold one video under two
    // titles and the two local scenes were merged) counts as "in the library" -- so it is not listed as missing in
    // "Missing from Catalog", not wishlisted/re-downloaded, and counts for the completeness ratio (api/scene_alias.py).
    function buildAliasPanel() {
      const { el, confirmInline } = window.SRW;
      const listEl = el('div');
      const aIn = el('input', { type: 'text', placeholder: 'StashDB scene id (uuid)', style: 'width: 22rem' });
      const lIn = el('input', { type: 'text', placeholder: 'Local scene id', style: 'width: 7rem' });
      const nIn = el('input', { type: 'text', placeholder: 'Note (optional)', style: 'width: 14rem' });
      const addBtn = el('button', { className: 'srw-btn', textContent: 'Add alias', onClick: async () => {
        const r = await window.SRW.runOp('compare_alias_add', { alias_scene_id: aIn.value.trim(), local_scene_id: lIn.value.trim(), note: nIn.value.trim() });
        if (r.error) { window.SRW.notify('Alias ids', r.error, 'error'); return; }
        window.SRW.notify('Alias ids', 'Alias recorded: that StashDB scene now counts as in the library.', 'success');
        aIn.value = ''; lIn.value = ''; nIn.value = ''; load();
      } });
      async function load() {
        const r = await window.SRW.runOp('compare_aliases');
        if (r.error) { listEl.textContent = `error: ${r.error}`; return; }
        if (!(r.aliases || []).length) { listEl.replaceChildren(el('div', { className: 'srw-hint', textContent: 'No aliases yet.' })); return; }
        listEl.replaceChildren(...r.aliases.map((a) => {
          const rm = el('button', { className: 'srw-btn srw-btn-sm srw-btn-danger', textContent: 'Remove' });
          rm.addEventListener('click', (e) => confirmInline(e.currentTarget, 'Remove this alias? That StashDB scene is listed as missing again.', async () => {
            await window.SRW.runOp('compare_alias_remove', { row_id: a.id }); load();
          }, { danger: true }));
          const isDup = a.kind === 'duplicate_entry';
          const resolvable = isDup && a.status !== 'open';
          const statusText = { open: 'watching for a StashDB merge', merged: 'StashDB merged it', gone: 'removed on StashDB' }[a.status] || a.status;
          const resolveBtn = resolvable ? el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Resolve',
            title: 'Use the StashDB data again and take this entry off the list',
            onClick: async (e) => {
              const btn = e.currentTarget;
              const prev = await window.SRW.runOp('compare_alias_resolve_preview', { row_id: a.id });
              if (prev.error) { window.SRW.notify('Duplicate entry', prev.error, 'error'); return; }
              confirmInline(btn, prev.text, async () => {
                const done = await window.SRW.runOp('compare_alias_resolve', { row_id: a.id });
                if (done.error) { window.SRW.notify('Duplicate entry', done.error, 'error'); return; }
                window.SRW.dismissedNotice('StashDB duplicate entry resolved', `Backup: ${done.backup}`,
                  () => window.SRW.runOp('compare_alias_resolve_undo', { applied: done.applied, row: done.row }), () => load());
                load();
              });
            } }) : null;
          return el('div', { className: 'srw-cmp-id' }, [
            el('div', {}, [
              isDup ? el('span', { className: `srw-film-class ${a.status === 'open' ? 'srw-film-class-unsure' : 'srw-film-class-sure'}`, textContent: 'StashDB duplicate',
                title: 'StashDB lists this scene twice under the same title. It is watched; when StashDB merges the entries you can resolve it here.' }) : null,
              isDup ? el('span', { className: 'srw-hint', textContent: ` ${statusText} ` }) : null,
              el('a', { href: `https://stashdb.org/scenes/${a.alias_scene_id}`, target: '_blank', rel: 'noopener noreferrer',
                textContent: a.title || a.alias_scene_id }), ` ${a.scene_date || ''} → local scene `,
              el('a', { href: `/scenes/${a.local_scene_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: `#${a.local_scene_id} ${a.local_title || ''}` })].filter(Boolean)),
            el('div', { className: 'srw-hint', textContent: a.note || '' }), resolveBtn, rm].filter(Boolean));
        }));
      }
      const det = el('details', { className: 'srw-panel' }, [
        el('summary', { textContent: 'Alias ids (same video under a second title) and StashDB duplicates' }),
        el('p', { className: 'srw-hint', textContent: 'A provider scene listed here counts as owned although no local scene carries its id.' }),
        el('div', { className: 'srw-toolbar' }, [aIn, lIn, nIn, addBtn]), listEl]);
      det.addEventListener('toggle', () => { if (det.open) load(); });
      return det;
    }

    // Real frames cut from the file (5 positions) -- the Stash screenshot/cover is the provider's image and says nothing about the file.
    function buildFramesStrip(s) {
      const strip = el('div', { className: 'srw-cmp-frames' });
      const btn = window.SRW.iconButton('button', 'faImages', 'Show frames from the file', { className: 'srw-btn srw-btn-sm',
        title: 'Five frames cut from THIS file (10/30/50/70/90 %). Stash’s own screenshot is only the provider’s cover image.',
        onClick: async () => {
          btn.disabled = true; strip.textContent = 'cutting frames…';
          const r = await window.SRW.runOp('compare_frames', { scene_id: s.id });
          btn.disabled = false;
          if (r.error) { strip.textContent = `error: ${r.error}`; return; }
          strip.replaceChildren(...(r.frames || []).map((f) => el('figure', { className: 'srw-cmp-frame' }, [
            f.data_url ? el('img', { src: f.data_url, alt: '' }) : el('div', { className: 'srw-hint', textContent: 'no frame' }),
            el('figcaption', { className: 'srw-hint', textContent: `${Math.floor(f.t / 60)}:${String(f.t % 60).padStart(2, '0')}` }),
          ])));
        } });
      return el('div', {}, [el('div', { className: 'srw-toolbar' }, [btn, el('span', { className: 'srw-hint', textContent: 'Stash’s cover image is the provider’s picture, not from this file.' })]), strip]);
    }

    // For a scene without a provider id: find out what it really is (exact fingerprints, then a name search), put
    // the provider's id + metadata on it, or clear what a wrong match left behind and mark it for review.
    function buildIdentifyPanel(p, s, tag) {
      const out = el('div', { className: 'srw-cmp-identify-out' });
      const stem = ((s.files && s.files[0] && s.files[0].path) || '').split('/').pop()
        .replace(/\.[a-z0-9]{2,4}$/i, '').replace(/\s*[-\[(]?\s*(WEBDL|WEBRip|HDTV|BluRay)[^\])]*[\])]?/i, '')
        .replace(/\s*-?\s*\d{4}-\d{2}-\d{2}/, '').replace(/[._]+/g, ' ').trim();
      const q = el('input', { type: 'text', value: stem, style: 'width: 18rem', title: 'Search text (pre-filled from the file name)' });

      const renderCandidates = (items, fingerprint) => {
        if (!items.length) { out.replaceChildren(el('div', { className: 'srw-hint', textContent: fingerprint ? 'No fingerprint match at any provider you set up.' : 'No results.' })); return; }
        out.replaceChildren(el('div', { className: 'srw-hint', textContent: fingerprint ? (items.some((c) => c.provider === 'theporndb')
          ? 'Fingerprint match at ThePornDB (its hash lookup is similarity-based, NOT guaranteed exact -- compare by playing before using it):'
          : `Exact fingerprint match at ${window.SRW.providerLabel(items[0].provider || 'stashdb')}:`) : 'Name search results (not verified -- compare by playing):' }),
          ...items.map((c) => {
            const useBtn = el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: `Use this for ${tag}` });
            if (c.held_by_scene) { useBtn.disabled = true; useBtn.textContent = `held by scene #${c.held_by_scene}`; }
            useBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
              `Put ${c.provider} scene “${c.title || '?'}” (${c.date || '?'}) on scene #${s.id}? Title, date, studio, performers and tags are replaced; the old state is saved to the backup file.`,
              async () => {
                const r = await window.SRW.runOp('compare_adopt', { id: p.id, scene_id: s.id, provider: c.provider, provider_id: c.provider_id });
                if (r.error) { window.SRW.notify('Compare scenes', r.error, 'error'); return; }
                window.SRW.notify('Compare scenes', `Scene #${s.id} is now “${r.scene.title}”. The next Organize run renames the file.${r.review_closed ? ' It left the Review list.' : ''}`, 'success');
                reopen(p);
              }));
            return el('div', { className: 'srw-cmp-id' }, [
              el('div', {}, [el('strong', { textContent: `${c.provider} ` }),
                c.url ? el('a', { href: c.url, target: '_blank', rel: 'noopener noreferrer', textContent: c.title || c.provider_id }) : (c.title || c.provider_id)]),
              el('div', { className: 'srw-hint', textContent: `${c.date || '?'} · ${c.studio || '?'} · ${(c.performers || []).join(', ') || '—'}${c.minutes ? ` · ${c.minutes} min` : ''}` }),
              useBtn,
            ]);
          }));
      };

      const idBtn = window.SRW.iconButton('button', 'faFingerprint', 'Identify by fingerprint', { className: 'srw-btn srw-btn-sm',
        title: 'Looks the file\'s hashes up at StashDB (then ThePornDB) -- exact matches only',
        onClick: async () => {
          idBtn.disabled = true; out.textContent = 'looking up…';
          const r = await window.SRW.runOp('compare_identify', { id: p.id, scene_id: s.id });
          idBtn.disabled = false;
          if (r.error) { out.textContent = `error: ${r.error}`; return; }
          window.SRW.keepResult(`lookup:${s.id}`, { kind: 'id', r });
          renderCandidates(r.matches || [], true);
        } });
      const renderSearch = (r) => {
        const errs = (r.results || []).filter((x) => x.error);
        renderCandidates((r.results || []).filter((x) => !x.error), false);
        errs.forEach((x) => out.appendChild(el('div', { className: 'srw-hint', textContent: `${x.provider}: ${x.error}` })));
      };
      const searchBtn = window.SRW.iconButton('button', 'faMagnifyingGlass', 'Search by name', { className: 'srw-btn srw-btn-sm',
        onClick: async () => {
          searchBtn.disabled = true; window.SRW.setBusy(out, 'searching…');
          const r = await window.SRW.runOp('compare_search', { id: p.id, scene_id: s.id, query: q.value });
          searchBtn.disabled = false;
          if (r.error) { out.textContent = `error: ${r.error}`; return; }
          window.SRW.keepResult(`lookup:${s.id}`, { kind: 'search', r, query: q.value });
          renderSearch(r);
        } });
      const resetBtn = el('button', { className: 'srw-btn srw-btn-sm srw-btn-danger', textContent: 'Reset metadata' });
      resetBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
        `Clear title, date, studio, performers, URLs and tags of scene #${s.id} and mark it “Review: Identify Scene”? Markers, rating and play history stay; the old state is saved to the backup file.`,
        async () => {
          const r = await window.SRW.runOp('compare_reset_metadata', { id: p.id, scene_id: s.id });
          if (r.error) { window.SRW.notify('Compare scenes', r.error, 'error'); return; }
          window.SRW.notify('Compare scenes', `Scene #${s.id} reset and marked for review.`, 'success');
          reopen(p);
        }, { danger: true }));
      // Stash's own scrapers (AdultEmpire, IAFD, data18 ...): one request through Stash, results shown, nothing applied unasked.
      const scraperSel = el('select', { title: 'Scraper installed in Stash' });
      window.SRW.runOp('compare_scrapers').then((r) => (r.scrapers || []).forEach((sc) => scraperSel.appendChild(
        el('option', { value: sc.id, textContent: `${sc.name}${sc.name_search ? ' (title)' : ''}${sc.url ? ' (URL)' : ''}` }))));
      const sq = el('input', { type: 'text', value: stem, style: 'width: 18rem', placeholder: 'title, or a page URL (IAFD, data18, AdultEmpire ...)' });
      // IAFD and data18 read a page URL only; the text field says so for the selected scraper.
      const nameSearch = {};
      window.SRW.runOp('compare_scrapers').then((r) => (r.scrapers || []).forEach((sc) => { nameSearch[sc.id] = sc.name_search; }));
      scraperSel.addEventListener('change', () => {
        sq.placeholder = nameSearch[scraperSel.value] === false ? 'page URL of the scene (this scraper has no title search)' : 'title, or a page URL';
      });
      const renderScrape = (r) => {
        if (!(r.results || []).length) { out.textContent = 'The scraper found nothing.'; return; }
        out.replaceChildren(el('div', { className: 'srw-hint', textContent: 'Scraper results (no provider id -- "Apply" only fills title/date/details/URL):' }),
          ...r.results.map((x) => {
            const ap = el('button', { className: 'srw-btn srw-btn-sm', textContent: `Apply to ${tag}` });
            ap.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
              `Put title “${x.title || '?'}”, date, details and URL on scene #${s.id}? Studio and performers stay as they are; the old state is saved.`,
              async () => {
                const r2 = await window.SRW.runOp('compare_apply_scrape', { id: p.id, scene_id: s.id, title: x.title, date: x.date, details: x.details, url: (x.urls || [])[0] });
                if (r2.error) { window.SRW.notify('Compare scenes', r2.error, 'error'); return; }
                window.SRW.notify('Compare scenes', `Scene #${s.id} updated.`, 'success'); reopen(p);
              }));
            return el('div', { className: 'srw-cmp-id' }, [
              el('div', {}, [el('strong', { textContent: x.title || '(no title)' }), ` ${x.date || ''} · ${x.studio || '?'} · ${(x.performers || []).join(', ') || '—'}`]),
              el('div', { className: 'srw-hint', textContent: (x.details || '').slice(0, 220) }),
              (x.urls || [])[0] ? el('a', { href: x.urls[0], target: '_blank', rel: 'noopener noreferrer', textContent: x.urls[0] }) : null, ap]);
          }));
      };
      const sBtn = window.SRW.iconButton('button', 'faWandMagicSparkles', 'Search via Stash scraper', { className: 'srw-btn srw-btn-sm', onClick: async () => {
        sBtn.disabled = true; out.textContent = 'asking the scraper…';
        const r = await window.SRW.runOp('compare_scrape', { scraper_id: scraperSel.value, query: sq.value });
        sBtn.disabled = false;
        if (r.error) { out.textContent = `error: ${r.error}`; return; }
        window.SRW.keepResult(`lookup:${s.id}`, { kind: 'scrape', r, query: sq.value });
        renderScrape(r);
      } });
      // The last lookup for this scene is shown again when the dialog is reopened or the page reloaded.
      const last = window.SRW.recallResult(`lookup:${s.id}`);
      if (last && last.value) {
        const v = last.value;
        if (v.kind === 'id') renderCandidates(v.r.matches || [], true);
        else if (v.kind === 'search') { q.value = v.query || q.value; renderSearch(v.r); }
        else if (v.kind === 'scrape') { sq.value = v.query || sq.value; renderScrape(v.r); }
        out.prepend(window.SRW.keptNote(last.at));
      }
      return el('div', { className: 'srw-cmp-identify' }, [
        el('div', { className: 'srw-hint', textContent: `${tag} has no provider id -- what is it?` }),
        el('div', { className: 'srw-toolbar' }, [idBtn]),
        el('div', { className: 'srw-toolbar' }, [q, searchBtn]),
        el('div', { className: 'srw-toolbar' }, [scraperSel, sq, sBtn]),
        el('div', { className: 'srw-toolbar' }, [resetBtn]),
        out,
      ]);
    }

    // ===== Review: Identify Scene (2026-10-02): single scenes carrying that Stash tag, same tools as the pairs =====
    const reviewStatus = el('span', { className: 'srw-key-status' });
    const reviewList = el('div');
    const reviewViewer = el('div', { className: 'srw-cmp-viewer' });
    let reviewScenes = [];
    let allReview = [];
    let reviewView = null;
    let reviewOpenId = null;
    // Open = not looked at / still to do; Later = looked at, cannot be resolved now (stays tagged in Stash).
    const REVIEW_FILTERS = [['open', 'Open'], ['later', 'Later'], ['ids', 'Has id'], ['', 'All']];
    let reviewFilter = 'open';
    const reviewFilterBtns = REVIEW_FILTERS.map(([value, label]) => {
      const b = el('button', { className: 'srw-btn srw-btn-sm', textContent: label });
      b.addEventListener('click', () => { reviewFilter = value; applyReviewFilter(); });
      return { value, label, b };
    });
    // Two tabs (2026-10-04): single scenes, and films in the movie folders (their review adds the ThePornDB movie tools below the player).
    // Settings > Content: only the tab of the area that is switched on (a single tab is not shown as a choice).
    const REVIEW_TABS = [['scene', 'Scenes'], ['movie', 'Movies & groups']].filter(([value]) => contentScope === 'both' || (contentScope === 'movies') === (value === 'movie'));
    let reviewTab = REVIEW_TABS[0][0];
    const reviewTabBtns = REVIEW_TABS.map(([value, label]) => {
      const b = el('button', { className: 'srw-btn srw-btn-sm' });
      b.addEventListener('click', () => { reviewTab = value; closeReview(); applyReviewFilter(); });
      return { value, label, b };
    });
    const applyReviewFilter = () => {
      const inTab = allReview.filter((x) => (x.kind || 'scene') === reviewTab);
      reviewScenes = inTab.filter((x) => (reviewFilter === 'ids' ? x.provider_ids > 0 : (!reviewFilter || x.status === reviewFilter)));
      const n = (st) => inTab.filter((x) => x.status === st).length;
      const counts = { open: n('open'), later: n('later'), ids: inTab.filter((x) => x.provider_ids > 0).length, '': inTab.length };
      reviewFilterBtns.forEach((f) => {
        f.b.textContent = `${f.label} (${counts[f.value]})`;
        f.b.classList.toggle('srw-btn-primary', f.value === reviewFilter);
      });
      reviewTabBtns.forEach((t) => {
        const all = allReview.filter((x) => (x.kind || 'scene') === t.value);
        t.b.textContent = `${t.label} (${all.filter((x) => x.status === 'open').length} open / ${all.length})`;
        t.b.classList.toggle('srw-btn-primary', t.value === reviewTab);
      });
      reviewStatus.textContent = ` ${reviewScenes.length} shown`;
      const identified = inTab.filter((x) => x.provider_ids > 0).length;
      closeIdentifiedBtn.hidden = !identified || reviewTab !== 'scene'; // not for films: an id alone does not finish a film (it needs its Group)
      closeIdentifiedBtn.textContent = `Close the ${identified} identified one(s)`;
      if (reviewView) reviewView.reload();
    };

    // Scenes that were identified some other way (they carry a provider id now) can leave the list in one go: the same "Review done" as in the viewer,
    // one scene after the other (each is backed up first).
    const closeIdentifiedBtn = el('button', { className: 'srw-btn', textContent: 'Close the identified ones', hidden: true,
      title: 'Takes the review tag off every scene of this list that has a provider id now' });
    closeIdentifiedBtn.addEventListener('click', () => {
      const todo = allReview.filter((x) => (x.kind || 'scene') === reviewTab && x.provider_ids > 0);
      window.SRW.confirmInline(closeIdentifiedBtn, `Take the tag “Review: Identify Scene” off ${todo.length} scene(s) that have a provider id? Everything else stays; each state is saved to the backup file first.`, async () => {
        let done = 0;
        for (const x of todo) {
          reviewStatus.textContent = ` closing ${done + 1} / ${todo.length}…`;
          const r = await window.SRW.runOp('compare_review_done', { scene_id: x.id });
          if (r.error) { window.SRW.notify('Review', `#${x.id}: ${r.error}`, 'error'); break; }
          done += 1;
        }
        closeReview();
        await loadReview();
        window.SRW.notify('Review', `${done} scene(s) removed from the review list.`, 'success');
      });
    });
    async function loadReview() {
      reviewStatus.textContent = ' loading…';
      const r = await window.SRW.runOp('compare_review_list');
      if (r.error) { reviewStatus.textContent = ` error: ${r.error}`; return; }
      allReview = r.scenes || [];
      if (!reviewView) {
        reviewView = window.SRW.buildFilteredPagedList({
          filters: ['search'], perPage: 20, showFilterBarFrom: 10,
          emptyText: 'No scene here. “Later” holds the cases you looked at and could not resolve yet; the tag is “Review: Identify Scene”.',
          fetchPage: window.SRW.localPageFetcher(() => reviewScenes, {
            text: (x) => `${x.title || ''} ${x.studio || ''} ${x.path || ''} ${x.id} ${(x.performers || []).join(' ')}`,
          }),
          renderItems: (items) => el('ul', { className: 'srw-release-list' }, items.map(buildReviewRow)),
        });
        reviewList.appendChild(reviewView.node);
      }
      applyReviewFilter();
      window.dispatchEvent(new Event('srw:counts-changed'));
    }

    // File name next to the scene it was given (v0.171.2): for the files that carry a provider id, so a wrong match shows up at a glance.
    const reviewWords = (text) => new Set((text || '').toLowerCase().replace(/\.[a-z0-9]{2,4}$/, '').replace(/\b(webdl|webrip|dvd|bluray|hdtv|\d{3,4}p)\b|\(\d{4}\)|[^a-z0-9 ]+/g, ' ')
      .split(/\s+/).filter((w) => w.length >= 3 && !/^\d+$/.test(w) && !['the', 'and', 'scene', 'part', 'episode', 'with', 'for'].includes(w)));
    function reviewCompare(x) {
      if (!(x.provider_ids > 0)) return null;
      const file = (x.path || '').split('/').pop();
      const a = reviewWords(file);
      const b = reviewWords(x.title);
      const same = [...a].some((w) => b.has(w));
      const meta = [x.title ? x.title : 'no title (id only)', x.date, x.studio].filter(Boolean).join(' — ');
      return el('div', { className: 'srw-review-compare' }, [
        el('span', { className: `srw-upgrade-reason ${same ? 'srw-upgrade-reason-ok' : 'srw-upgrade-reason-vr_low_quality'}`, textContent: !x.title ? 'no metadata' : (same ? 'names share words' : 'names differ') }),
        el('div', { className: 'srw-hint', textContent: `File:  ${file}` }),
        el('div', { className: 'srw-hint', textContent: `Scene: ${meta}` }),
      ]);
    }

    // The row layout of the Wishlist / Stuck lists (v0.240.1): thumbnail, text on the left, the buttons in one column on the right.
    function buildReviewRow(x) {
      const thumb = el('a', { className: 'srw-release-thumb-link', href: `/scenes/${x.id}`, target: '_blank', rel: 'noopener noreferrer', title: 'Open the scene in Stash (new tab)' },
        [el('img', { className: 'srw-release-thumb', src: `/scene/${x.id}/screenshot`, alt: '', loading: 'lazy' })]);
      return window.SRW.arrangeReleaseRow(el('li', { className: 'srw-release-item' }, [
        thumb,
        el('span', { className: 'srw-release-title', textContent: x.title || (x.path || '').split('/').pop() || `Scene ${x.id}` }),
        el('span', { className: 'srw-upgrade-reason srw-upgrade-reason-vr_2d', textContent: x.status === 'later' ? 'later' : (x.provider_ids ? 'has id' : 'no id') }),
        el('span', { className: 'srw-release-date', textContent: `#${x.id} — ${x.height ? `${x.height}p` : '?'}, ${x.minutes == null ? '?' : x.minutes} min, ${x.mb == null ? '?' : x.mb} MB, `
          + `${x.markers} marker(s)${(x.performers || []).length ? `, ${x.performers.join(', ')}` : ''}` }),
        reviewCompare(x),
        el('div', { className: 'srw-hint srw-maintenance-file-path', textContent: x.path || '' }),
        el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Review', onClick: () => openReview(x.id) }),
      ]), {});
    }

    // The review opens in a modal (v0.240.1) over the list instead of below it; closing it (x, Escape, a click beside it, or any finished action) stops the players.
    let reviewModal = null;
    const resetReview = () => {
      reviewViewer.querySelectorAll('video').forEach((v) => { try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) { /* gone already */ } });
      reviewViewer.replaceChildren(); reviewOpenId = null;
    };
    function closeReview() {
      const m = reviewModal; reviewModal = null;
      resetReview();
      if (m) m.close();
    }
    async function openReview(id) {
      reviewOpenId = id;
      reviewViewer.replaceChildren(el('p', { className: 'srw-hint', textContent: 'Loading the scene and its provider entries…' }));
      if (!reviewModal) {
        const x = allReview.find((i) => i.id === id) || {};
        reviewModal = window.SRW.openModal('Review: Identify Scene', reviewViewer, {
          size: 'modal-xl srw-modal-wide',
          subtitle: window.SRW.sceneSubtitle({ title: x.title || (x.path || '').split('/').pop() || `Scene ${id}`, cast: (x.performers || []).map((name) => ({ name })),
            studio_name: x.studio, scene_date: x.date }),
          onClose: () => { reviewModal = null; resetReview(); },
        });
      }
      const s = await window.SRW.runOp('compare_review_scene', { scene_id: id });
      if (reviewOpenId !== id) return;
      if (s.error) { reviewViewer.replaceChildren(el('p', { className: 'srw-hint', textContent: `error: ${s.error}` })); return; }
      renderReview(s);
    }

    // Film review (2026-10-04): the folder of this scene (all its ungrouped parts), what the earlier ThePornDB runs found, a ThePornDB movie
    // search, and "create Group" -- GROUP data only (studio only when a local one exists), every part of the folder goes in.
    function buildMoviePanel(s) {
      const box = el('div', { className: 'srw-panel', style: 'padding: .6rem; margin-top: .6rem' }, [
        el('h5', { textContent: 'Movie / Group' }), el('div', { className: 'srw-hint', textContent: 'Reading the film folder…' })]);
      window.SRW.runOp('compare_movie_info', { scene_id: s.id }).then((info) => {
        if (info.error) { box.replaceChildren(el('h5', { textContent: 'Movie / Group' }), el('div', { className: 'srw-hint', textContent: info.error })); return; }
        if (info.grouped) {
          // Already in a Group: only its studio is left to decide (e.g. a studio that was linked by a similar name).
          const studioIn = el('input', { type: 'text', placeholder: 'studio name (an existing one is used, otherwise it is created)', style: 'width: 24rem' });
          const finish = (r, text) => {
            if (r.error) { window.SRW.notify('Movies', r.error, 'error'); return; }
            window.SRW.notify('Movies', text, 'success'); closeReview(); loadReview();
          };
          const setBtn = el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Set studio' });
          setBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
            `Set the studio of the Group(s) to “${studioIn.value}”? A studio of that exact name is used, otherwise it is created.`,
            async () => finish(await window.SRW.runOp('compare_group_studio', { scene_id: s.id, name: studioIn.value }), `Studio set to ${studioIn.value}.`)));
          const clearBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Remove the studio from the Group' });
          clearBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget, 'Remove the studio from the Group(s) of this film?',
            async () => finish(await window.SRW.runOp('compare_group_studio', { scene_id: s.id, clear: true }), 'Studio removed from the Group.')));
          box.replaceChildren(el('h5', { textContent: 'Movie / Group' }),
            el('div', { className: 'srw-hint', textContent: `This film is already in ${info.grouped.map((g) => `“${g.name}” (studio: ${g.studio || 'none'})`).join(', ')}.` }),
            el('div', { className: 'srw-toolbar' }, [studioIn, setBtn, clearBtn]));
          return;
        }
        const done = (r) => {
          window.SRW.notify('Movies', `Group “${r.name}” created with ${r.scenes} scene(s)${(r.notes || []).length ? ` (${r.notes.join('; ')})` : ''}.`, 'success');
          closeReview(); loadReview();
        };
        const candidateRow = (c) => {
          const btn = el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Create group from this movie' });
          btn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
            `Create the Group “${c.title}” (${c.year || '?'}) from ThePornDB and put ${info.scene_ids.length} scene(s) of this folder in it? Group data only; the studio is set only when a local one exists.`,
            async () => {
              btn.disabled = true;
              const r = await window.SRW.runOp('compare_movie_create', { scene_id: s.id, tpdb_id: c.id });
              btn.disabled = false;
              if (r.error) { window.SRW.notify('Movies', r.error, 'error'); return; }
              done(r);
            }));
          const fit = c.length_ok === undefined ? (c.minutes && info.minutes && Math.abs(c.minutes / info.minutes - 1) <= 0.10) : c.length_ok;
          return el('div', { className: 'srw-cmp-id' }, [
            el('div', {}, [el('strong', { textContent: c.title || '?' }),
              el('span', { className: 'srw-hint', textContent: ` ${c.year || '?'} · ${c.minutes ? `${c.minutes} min` : 'no length'} (folder ${info.minutes} min)${fit ? ' ✓ length matches' : ''} · ${c.site || '?'}${c.from ? ` · ${c.from}` : ''} ` })]),
            btn]);
        };
        const stored = el('div', {}, (info.stored || []).length
          ? [el('div', { className: 'srw-hint', textContent: 'Found by the earlier ThePornDB runs (no new request):' }), ...info.stored.map(candidateRow)]
          : [el('div', { className: 'srw-hint', textContent: 'The earlier ThePornDB runs found nothing for this folder.' })]);
        const sameGroups = el('div', {}, (info.same_title_groups || []).length ? [
          el('div', { className: 'srw-hint', textContent: 'A Group with this title already exists -- put the film into it:' }),
          ...info.same_title_groups.map((g) => {
            const btn = el('button', { className: 'srw-btn srw-btn-sm srw-btn-primary', textContent: 'Add to this group' });
            btn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
              `Put ${info.scene_ids.length} scene(s) of this folder into the existing Group “${g.name}”?`,
              async () => {
                btn.disabled = true;
                const r = await window.SRW.runOp('compare_group_add', { scene_id: s.id, group_id: g.id });
                btn.disabled = false;
                if (r.error) { window.SRW.notify('Movies', r.error, 'error'); return; }
                window.SRW.notify('Movies', `Added to the Group “${r.name}”.`, 'success');
                closeReview(); loadReview();
              }));
            return el('div', { className: 'srw-cmp-id' }, [
              el('div', {}, [el('a', { href: `/groups/${g.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: g.name }),
                el('span', { className: 'srw-hint', textContent: ` ${g.year || '?'} · ${g.minutes ? `${g.minutes} min` : 'no length'} · ${g.scenes} scene(s)${g.studio ? ` · ${g.studio}` : ''}` })]), btn]);
          })] : []);
        const results = el('div');
        const q = el('input', { type: 'text', value: info.title, style: 'width: 20rem', title: 'ThePornDB movie search text (pre-filled from the folder name)' });
        const searchBtn = window.SRW.iconButton('button', ['faMagnifyingGlass', 'faFilm'], 'Search ThePornDB movies', { className: 'srw-btn srw-btn-sm',
          onClick: async () => {
            searchBtn.disabled = true; results.textContent = 'asking ThePornDB…';
            const r = await window.SRW.runOp('compare_movie_search', { scene_id: s.id, q: q.value });
            searchBtn.disabled = false;
            if (r.error) { results.textContent = `error: ${r.error}`; return; }
            window.SRW.keepResult(`moviesearch:${s.id}`, { r, q: q.value });
            showMovieResults(r);
          } });
        const showMovieResults = (r) => {
          if (!(r.results || []).length) { results.textContent = `ThePornDB has no movie for “${r.query}”.`; return; }
          results.replaceChildren(...r.results.map(candidateRow));
        };
        const lastMovies = window.SRW.recallResult(`moviesearch:${s.id}`);
        if (lastMovies && lastMovies.value) { q.value = lastMovies.value.q || q.value; showMovieResults(lastMovies.value.r); results.prepend(window.SRW.keptNote(lastMovies.at)); }
        const nameInput = el('input', { type: 'text', value: info.title, style: 'width: 20rem', title: 'Name of the Group' });
        const manualBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Create group from the folder name' });
        manualBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
          `Create the Group “${nameInput.value}” without provider data and put ${info.scene_ids.length} scene(s) of this folder in it?`,
          async () => {
            manualBtn.disabled = true;
            const r = await window.SRW.runOp('compare_movie_create_manual', { scene_id: s.id, name: nameInput.value });
            manualBtn.disabled = false;
            if (r.error) { window.SRW.notify('Movies', r.error, 'error'); return; }
            done(r);
          }));
        box.replaceChildren(
          el('h5', { textContent: `Movie / Group — ${info.title}${info.year ? ` (${info.year})` : ''}` }),
          el('div', { className: 'srw-hint srw-maintenance-file-path', textContent: `${info.folder}\n${info.videos.length} video(s), ${info.minutes} min: ${info.videos.map((v) => `${v.file} (${v.minutes})`).join(', ')}` }),
          sameGroups,
          stored,
          el('div', { className: 'srw-toolbar' }, [q, searchBtn]), results,
          el('div', { className: 'srw-toolbar' }, [nameInput, manualBtn]));
      });
      return box;
    }

    function renderReview(s) {
      const videos = [];
      // p.id 0 = "no pair": the backend then checks the review tag instead of pair membership.
      // After an action the list is read again: a scene that was identified has lost its tag and is gone (v0.171.0); otherwise it stays open.
      const col = buildColumn({ id: 0, refresh: async () => { await loadReview(); if (allReview.some((x) => x.id === s.id)) openReview(s.id); else { closeReview(); } } }, 'review', s, videos);
      const each = (fn) => videos.forEach((pl) => { try { fn(pl); } catch (e) { /* not ready yet */ } });
      const bar = el('div', { className: 'srw-toolbar srw-cmp-bar' }, [
        window.SRW.iconButton('button', 'faPlay', 'Play', { className: 'srw-btn srw-btn-sm', onClick: () => each((pl) => { const pr = pl.video.play(); if (pr && pr.catch) pr.catch(() => {}); }) }),
        window.SRW.iconButton('button', 'faPause', 'Pause', { className: 'srw-btn srw-btn-sm', onClick: () => each((pl) => pl.video.pause()) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: '−10 s', onClick: () => each((pl) => pl.seek(pl.time() - 10)) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: '+10 s', onClick: () => each((pl) => pl.seek(pl.time() + 10)) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: '+60 s', onClick: () => each((pl) => pl.seek(pl.time() + 60)) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Jump to 50 %', onClick: () => each((pl) => pl.seek(pl.total * 0.5)) }),
      ]);
      const setLater = async (later) => {
        const r = await window.SRW.runOp('compare_review_status', { scene_id: s.id, status: later ? 'later' : 'open' });
        if (r.error) { window.SRW.notify('Review', r.error, 'error'); return; }
        window.SRW.notify('Review', later ? `Scene #${s.id} put on “Later” (still tagged in Stash).` : `Scene #${s.id} is open again.`, 'success');
        closeReview(); loadReview();
      };
      const laterBtn = window.SRW.iconButton('button', 'faClock', 'Later (unresolved)', {
        title: 'Later (unresolved): looked at, cannot be matched now -- stays tagged in Stash, leaves the Open list', onClick: () => setLater(true) });
      const reopenBtn = el('button', { className: 'srw-btn', textContent: 'Reopen', onClick: () => setLater(false) });
      const parkBtn = window.SRW.iconButton('button', ['faFolderOpen', 'faQuestion'], 'Move to _UNMATCHED', {
        title: 'Parks the file in scenes/_UNMATCHED (old sub folders kept) and takes the review tag off, so it leaves this list' });
      parkBtn.hidden = (s.files || []).some((f) => (f.path || '').includes('/_UNMATCHED/')); // already parked (in any _UNMATCHED folder)
      parkBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
        `Move the file of scene #${s.id} to scenes/_UNMATCHED/? The scene stays in Stash (path changes, a scan follows) and the review tag is removed.`,
        async () => {
          parkBtn.disabled = true;
          const r = await window.SRW.runOp('compare_review_unmatched', { scene_id: s.id });
          parkBtn.disabled = false;
          if (r.error) { window.SRW.notify('Review', r.error, 'error'); return; }
          window.SRW.notify('Review', `Moved to ${r.path}`, 'success');
          closeReview(); loadReview();
        }));
      const doneBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Review done' });
      doneBtn.addEventListener('click', (e) => window.SRW.confirmInline(e.currentTarget,
        `Take the tag “Review: Identify Scene” off scene #${s.id}? Everything else on the scene stays; the state is saved to the backup file.`,
        async () => {
          const r = await window.SRW.runOp('compare_review_done', { scene_id: s.id });
          if (r.error) { window.SRW.notify('Review', r.error, 'error'); return; }
          window.SRW.notify('Review', `Scene #${s.id} removed from the review list.`, 'success');
          closeReview(); loadReview();
        }));
      // (replaceChildren turns a null into the text "null": the film panel is left out of the list for single scenes)
      reviewViewer.replaceChildren(...[
        bar,
        el('div', { className: 'srw-cmp-grid srw-cmp-grid-one' }, [col]),
        (allReview.find((x) => x.id === s.id) || {}).kind === 'movie' ? buildMoviePanel(s) : null,
        el('div', { className: 'srw-toolbar' }, [doneBtn, (allReview.find((x) => x.id === s.id) || {}).status === 'later' ? reopenBtn : laterBtn, parkBtn,
          el('button', { className: 'srw-btn', textContent: 'Close', onClick: () => { closeReview(); } })]),
      ].filter(Boolean));
    }

    const reviewSection = el('details', { className: 'srw-settings-section srw-panel srw-compare' }, [
      el('summary', { textContent: 'Review: Identify Scene' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Scenes and films marked with the Stash tag “Review: Identify Scene” (wrong matches that were reset, cases no provider knows). The tab “Movies & groups” holds the films of the movie folders: ThePornDB movie search and “create group” come with the player. '
          + 'Play the file, look at real frames, identify by fingerprint or name, search through a Stash scraper, or put a provider id on it. '
          + '“Review done” takes the tag off. Every change is saved to compare_removed_ids.jsonl and History first.',
      }),
      REVIEW_TABS.length > 1 ? el('div', { className: 'srw-subtabs' }, reviewTabBtns.map((t) => t.b)) : null,
      el('div', { className: 'srw-toolbar' }, [el('span', { className: 'srw-view-toggle' }, reviewFilterBtns.map((f) => f.b)), window.SRW.iconButton('button', 'faRotate', 'Reload', { className: 'srw-btn', title: 'Reload: ask again, the list shown is a kept one', onClick: () => loadReview() }), closeIdentifiedBtn, reviewStatus]),
      reviewList,
    ]);
    // Only the part that is shown is loaded (each area is its own page now).
    if (part === 'review') {
      reviewSection.open = true;
      loadReview();
      return reviewSection;
    }
    load();
    return section;
  }

  window.SRW_Maintenance = { render };
})();
