/**
 * StashReleaseManager - Settings/watchlist page
 * Reachable at /plugins/stash-release-manager/settings.
 *
 * Watchlist membership is NOT managed here (changed 2026-09-11, see
 * PROGRESS.md's "Favorite-driven watchlist" section) -- it's synced
 * automatically from Stash's own Favorite flag on Studios/Performers
 * immediately before every scan (scheduled or manual). Star/unstar an
 * entity in Stash itself to watch/unwatch it; this page only shows the
 * current watchlist read-only, it can't add or remove entries anymore
 * (an earlier version had an add-form and per-row Remove button here --
 * removed because either one would just get silently undone by the
 * next scan's sync if the entity's favorite status in Stash disagreed,
 * which was confusing rather than useful).
 *
 * Since 2026-09-26 the watchlist can follow a dedicated Stash tag
 * instead of Favorites ("Watchlist source" block on the Watchlist tab,
 * api/watchlist_source.py) -- same read-only list either way.
 */
(function () {
  'use strict';

  // Tabbed layout (added 2026-09-19, user's own request -- "categories
  // like Whisparr's Settings page" -- after the flat stack of 7+
  // boxes plus the whole watchlist browser on one page became hard to
  // navigate). Deliberately groups whole EXISTING sections into tabs
  // rather than splitting each one apart into smaller pieces -- every
  // connection box still carries its own embedded sync-job
  // subsections (StashDB favorites sync, ThePornDB identify, Whisparr
  // quality profiles/safety sweep/import status), matching the user's
  // own 2026-09-12 decision to group a connection's settings under
  // that connection's own box instead of scattering them across
  // top-level sections -- this reorg is about how many boxes are
  // visible/scrolled through at once, not re-litigating that grouping.
  const SETTINGS_TABS = [
    { key: 'general', icon: 'faSliders', label: 'General' },
    { key: 'connections', icon: 'faPlug', label: 'Connections' },
    { key: 'search_quality', icon: 'faRss', label: 'Search & Quality' },
    { key: 'import_naming', icon: 'faFileLines', label: 'Import & Naming' },
    { key: 'jobs', icon: 'faClock', label: 'Jobs' },
  ];
  // Hashes of the tab layout before v0.145.0 (bookmarks, browser history).
  // (#watchlist is forwarded to the Watchlist page in render().)
  const OLD_TAB_KEYS = { metadata_providers: 'connections', indexers: 'search_quality', schedule: 'general', file_naming: 'import_naming' };

  // Cuts a built section at its sub-headings: the part before the first one stays in `section`, each following part becomes its own
  // .srw-settings-section (heading turned into an h4). Returns them by heading text. Lets one builder feed several tabs.
  function splitSection(section, headings) {
    const parts = {};
    let target = null;
    [...section.children].forEach((child) => {
      if (child.classList && child.classList.contains('srw-subsection-heading') && headings.includes(child.textContent)) {
        target = window.SRW.el('div', { className: 'srw-settings-section' }, [window.SRW.el('h4', { textContent: child.textContent })]);
        parts[child.textContent] = target;
        child.remove();
      } else if (target) {
        target.appendChild(child);
      }
    });
    return parts;
  }

  // Wraps a sub-heading and the controls after it (up to the next sub-heading) into one div, so the block can be handled as a unit.
  function wrapSubsection(section, headingText) {
    const kids = [...section.children];
    const start = kids.findIndex((c) => c.classList && c.classList.contains('srw-subsection-heading') && c.textContent === headingText);
    if (start < 0) return null;
    const wrap = window.SRW.el('div');
    section.insertBefore(wrap, kids[start]);
    for (let i = start; i < kids.length; i += 1) {
      if (i > start && kids[i].classList && kids[i].classList.contains('srw-subsection-heading')) break;
      wrap.appendChild(kids[i]);
    }
    return wrap;
  }

  // Greying out settings that do nothing under the chosen Whisparr scope: see SRW.createScopeStateRegistry() in core.js (shared with Maintenance).
  const createScopeStateRegistry = () => window.SRW.createScopeStateRegistry();

  async function render(container) {
    const { el } = window.SRW;
    container.innerHTML = '';
    container.appendChild(window.SRW.buildPageHeader('settings'));

    // Fetched once here instead of once per section builder (each used
    // to call settings_get_all independently -- six redundant round-
    // trips) -- 2026-09-12.
    const [settings, effective] = await Promise.all([
      window.SRW.runOp('settings_get_all'),
      window.SRW.runOp('settings_get_effective_connections'),
    ]);
    await window.SRW.loadProviders();
    const secretPlaceholder = (isSet) => (isSet ? '(active, from .env — enter here to override)' : '(not set)');

    const tabContent = el('div');

    // Every panel is built up front, not lazily on first tab click --
    // simpler than re-fetching/re-building on every switch (e.g. the
    // Whisparr quality-profile dropdowns would otherwise re-hit the API
    // each time), and shown/hidden via `hidden` (property assignment,
    // not setAttribute -- see the same gotcha noted elsewhere in this
    // file for boolean HTML attributes).
    const panels = {};

    // General: what to look after and the overall workflow, plus housekeeping (releases page size, recycle bin, history log).
    const scopeState = createScopeStateRegistry();
    panels.general = el('div');
    panels.general.appendChild(el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Setup guide' }),
      el('p', { className: 'srw-hint', textContent: 'The entries a new installation needs (StashDB, search, download client, quality profile), step by step.' }),
      el('a', { className: 'srw-btn', href: '/plugins/stash-release-manager/setup', textContent: 'Open the setup guide' }),
    ]));
    panels.general.appendChild(buildContentScopeSection(settings));
    // Whisparr scope: only when Whisparr is configured. Without it the sidecar does everything (the backend forces 'no_whisparr').
    const whisparrConfigured = !!effective.whisparr_url && !!effective.whisparr_api_key_set;
    const importSection = buildFileImportSection(whisparrConfigured ? settings : { ...settings, file_import_source: 'sidecar_sabnzbd' });
    const importParts = splitSection(importSection, ['Fingerprints', 'Recycle bin', 'History log']);
    if (whisparrConfigured) {
      const workflow = el('div', { className: 'srw-settings-section' }, [
        el('h4', { textContent: 'Workflow (Whisparr scope)' }),
        buildWhisparrScopeSection(settings, (src, preset) => { importSection.applySource(src); scopeState.apply(preset); }),
      ]);
      // Stage B of Whisparr for movies (v0.158.0): off until the dry run in Maintenance > Movies & groups > Whisparr has been seen.
      const fileAuto = el('div', {}, [el('p', { className: 'srw-subsection-heading', textContent: 'Imported movies' })]);
      appendSettingRow(fileAuto, {
        label: 'Tell Whisparr the folder of imported movies', key: 'film_whisparr_file_auto', value: settings.film_whisparr_file_auto === 'true' ? 'true' : 'false',
        options: [['false', 'Off'], ['true', 'On']],
        hint: 'After a movie is imported, Whisparr is told its folder and re-reads it (nothing is moved or deleted), so it shows the movie as having a file. '
          + 'Keep it off until you have seen the dry run (Maintenance > Movies & groups > Whisparr > “Imported movies”); the movies imported before are caught up there by hand.',
      });
      workflow.appendChild(fileAuto);
      // Stage D (v0.160.0): wanted movies whose file is already in the library -- the clear cases are used automatically after the Whisparr sync.
      const libAuto = el('div', {}, [el('p', { className: 'srw-subsection-heading', textContent: 'Movies already in the library' })]);
      appendSettingRow(libAuto, {
        label: 'Use the existing file automatically', key: 'film_library_auto', value: settings.film_library_auto === 'true' ? 'true' : 'false',
        options: [['false', 'Off'], ['true', 'On']],
        hint: 'A wanted movie whose file Whisparr / Stash already have (length fits, at most one group) is linked to it instead of being downloaded again. '
          + 'Nothing is downloaded, moved or deleted. Keep it off until you have looked at the list (Maintenance > Movies & groups > Already in the library); movies where the length does not fit always wait for you.',
      });
      workflow.appendChild(libAuto);
      scopeState.add(libAuto, ['no_whisparr'], 'Not used: with “No Whisparr” the movies are not looked up in Whisparr, so this check has nothing to go on.');
      scopeState.add(fileAuto, ['no_whisparr', 'full_whisparr'], 'Not used: with “No Whisparr” nothing is reported, with “Full Whisparr management” Whisparr imports the movies itself.');
      panels.general.appendChild(workflow);
    }
    // Watchlist source (v0.148.0): configuration, so it lives here; the Watchlist page only shows it with a link.
    watchlistMode = settings.watchlist_source === 'tag' ? 'tag' : 'favorites';
    watchlistTagName = settings.watchlist_tag_name || 'Watchlist';
    defaultWindowFrom = settings.default_window_from || '';
    panels.general.appendChild(buildWatchlistSourceSection(settings, () => render(container)));
    panels.general.appendChild(buildReleasesPageSection(settings));
    if (importParts['Recycle bin']) panels.general.appendChild(importParts['Recycle bin']);
    if (importParts['History log']) panels.general.appendChild(importParts['History log']);
    panels.general.appendChild(buildTransferSection());

    // Connections: every program with its URL/key/check and the settings that exist only because of it.
    panels.connections = el('div');
    panels.connections.appendChild(await buildMetadataProvidersSection(settings, effective, secretPlaceholder));
    const connectionsSection = await buildConnectionsSection(settings, effective, secretPlaceholder);
    panels.connections.appendChild(connectionsSection);
    const whisparrBox = [...connectionsSection.querySelectorAll('.srw-settings-section')].find((x) => (x.querySelector(':scope > h4') || {}).textContent === 'Whisparr');
    if (whisparrBox) {
      scopeState.add(wrapSubsection(whisparrBox, 'Quality profiles'), ['no_whisparr'], 'Not used: with “No Whisparr” nothing is handed to Whisparr.');
      scopeState.add(wrapSubsection(whisparrBox, 'Import status'), ['no_whisparr'], 'Not used: with “No Whisparr” there is nothing in Whisparr to check.');
    }
    panels.connections.appendChild(await buildDownloadClientsSection());

    panels.search_quality = el('div');
    const searchBackendSection = await buildSearchBackendSection();
    appendSettingRow(searchBackendSection, {
      label: 'Maximum grabs per day (all kinds)', key: 'max_grabs_per_day', value: settings.max_grabs_per_day || '30', min: '1', max: '1000', step: '1',
      hint: 'Hard cap on downloads this sidecar starts per day, whatever triggers them. Protects the indexer account against a runaway loop.',
    });
    panels.search_quality.appendChild(searchBackendSection);
    panels.search_quality.appendChild(await buildIndexersSection());
    panels.search_quality.appendChild(await buildQualityProfilesSection(settings));

    panels.import_naming = el('div');
    panels.import_naming.appendChild(importSection);
    if (importParts.Fingerprints) {
      panels.import_naming.appendChild(importParts.Fingerprints);
      scopeState.add(importParts.Fingerprints, ['full_whisparr'], 'Not used: with “Full Whisparr management” Whisparr imports scenes itself, so there is no “Import failed” check here.');
    }
    panels.import_naming.appendChild(el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Safety check' }),
      el('p', { className: 'srw-hint', textContent: 'Looks for what could go wrong when files are moved or renamed: another plugin that renames files when a scene is updated '
        + '(renamerOnUpdate and relatives would move every file again after each move), target folders that are not in a Stash library, formats that give every file the same name, '
        + 'the Whisparr scope, and files that were moved again by something else. Nothing is changed. Import folder, Organize library and the Organized hook refuse to move files '
        + 'while another renamer is enabled (you can continue anyway for a browser session).' }),
      window.SRW.buildSafetyPanel(),
    ]));
    panels.import_naming.appendChild(buildOrganizedHookSection(settings));
    panels.import_naming.appendChild(buildFileNamingSection(settings));

    panels.jobs = el('div');
    const scheduleSection = buildScheduleSection(settings);
    const scheduleParts = splitSection(scheduleSection, [
      'Indexer search re-scan', 'Movie search (Movies wishlist)', 'New movies of watched studios (Movies wishlist > Suggestions)',
    ]);
    panels.jobs.appendChild(scheduleSection);
    const upgradeCard = buildUpgradeSearchSection(settings);
    Object.entries(scheduleParts).forEach(([name, part]) => {
      panels.jobs.appendChild(part);
      if (name === 'Indexer search re-scan') panels.jobs.appendChild(upgradeCard); // the upgrade search rides on that loop
    });
    scopeState.add(upgradeCard, ['full_whisparr'], 'Not used: with “Full Whisparr management” the indexer search stops before the upgrades, so there is no automatic upgrade search.');
    scopeState.add(scheduleParts['Indexer search re-scan'], ['full_whisparr'],
      'Not used: with “Full Whisparr management” Whisparr searches for scenes itself. Movies are searched by the movie search below.');
    panels.jobs.appendChild(buildInactiveSettingsSection(settings));
    panels.jobs.appendChild(buildJobsOverviewSection(settings, whisparrConfigured, (key) => {
      if (key === 'watchlist') window.location.assign('/plugins/stash-release-manager/watchlist'); else showTab(key);
    }));

    applyContentScope(panels, await window.SRW.getContentScope());
    scopeState.apply(whisparrConfigured ? (settings.whisparr_scope_preset || 'bookkeeping_only') : 'no_whisparr');
    SETTINGS_TABS.forEach((t) => tabContent.appendChild(panels[t.key]));

    // Area list on the left (deep link: #<key> in the address, so reload/back keep the area).
    const side = window.SRW.buildSideNav(SETTINGS_TABS, SETTINGS_TABS[0].key, (key) => {
      showTab(key);
      // Full path, not just '#key': Stash's <base href="/"> would resolve a bare fragment against the site root and the
      // router would then drop this page (v0.98.3).
      try { history.replaceState(null, '', `${location.pathname}${location.search}#${key}`); } catch (e) { /* ignore */ }
    });
    function showTab(key) {
      SETTINGS_TABS.forEach((t) => { panels[t.key].hidden = t.key !== key; });
      side.setActive(key);
    }
    container.appendChild(side.layout(tabContent));
    const wantedRaw = (location.hash || '').slice(1);
    if (wantedRaw === 'watchlist') { window.location.replace('/plugins/stash-release-manager/watchlist'); return; } // moved to its own page (v0.147.0)
    const wanted = OLD_TAB_KEYS[wantedRaw] || wantedRaw;
    showTab(SETTINGS_TABS.some((t) => t.key === wanted) ? wanted : SETTINGS_TABS[0].key);

  }

  // One labeled input + Save button, wired to a single settings_set key.
  // Every field below is stored in this plugin's own DB (editable here,
  // no container rebuild needed) -- takes precedence over the matching
  // api/.env value when set, falls back to it when left empty. See
  // routers/settings.py's get_setting_value().
  // Stash's own connection is not a field of the Connections list (2026-09-20): Stash itself is always required and shouldn't be
  // removable alongside the optional backends (Whisparr/Prowlarr/...). Since v0.186.0 it is not a plugin setting either: the
  // sidecar takes the API key (and the StashDB / ThePornDB / other stash-box keys) from Stash itself, see
  // stash_release_manager_backend.py's _sync_from_stash(); only the Stash URL is confirmed in the setup guide's Stash step.
  //
  // `onSaved(key, value)` (added 2026-09-20) fires after a successful
  // save -- only the URL/API-key fields of each connection type pass it
  // through (see buildStashDBConnectionSection etc. below), so the new
  // ARR-style Connections list can move a type between "configured" and
  // "not configured" without a full page reload. Plain settings fields
  // (sync intervals, quality-profile dropdowns, ...) don't need it --
  // saving one of those doesn't change whether a connection counts as
  // configured.
  function buildConnectionField(section, { label, key, value, placeholder, isSecret, onSaved }) {
    const { el } = window.SRW;
    const input = el('input', {
      type: isSecret ? 'password' : 'text', autocomplete: 'off',
      placeholder: placeholder || '', value: value || '',
    });
    const status = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {}, [`${label}: `, input]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key, value: input.value });
        status.textContent = ' saved';
        setTimeout(() => { status.textContent = ''; }, 2000);
        if (onSaved) onSaved(key, input.value);
      },
    }));
    section.appendChild(status);
    section.appendChild(el('br'));
  }

  // URL and API key of one connection in ONE form (v0.214.0): type both, one Save writes both (only what was changed, so a key that comes from
  // api/.env and shows as a placeholder is never overwritten by an empty field). "Check connection" saves pending changes first and then asks the
  // sidecar to ping the service, so "type both, click once" works. `check` = {name, key} of the service, or null.
  function buildConnectionPair(section, { url, apiKey, check, onSaved }) {
    const { el } = window.SRW;
    const urlInput = el('input', { type: 'text', autocomplete: 'off', placeholder: url.placeholder || '', value: url.value || '' });
    const keyInput = el('input', { type: 'password', autocomplete: 'off', placeholder: apiKey.placeholder || '', value: apiKey.value || '' });
    const initial = { [url.key]: url.value || '', [apiKey.key]: apiKey.value || '' };
    const status = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {}, [`${url.label || 'URL'}: `, urlInput]));
    section.appendChild(el('br'));
    section.appendChild(el('label', {}, [`${apiKey.label || 'API key'}: `, keyInput]));
    section.appendChild(el('br'));
    async function savePending() {
      const changed = [[url.key, urlInput], [apiKey.key, keyInput]].filter(([key, input]) => input.value !== initial[key]);
      for (const [key, input] of changed) {
        const r = await window.SRW.runOp('settings_set', { key, value: input.value });
        if (r && r.error) throw new Error(r.error);
        initial[key] = input.value;
      }
      return changed;
    }
    function notify(changed) {
      if (onSaved) changed.forEach(([key, input]) => onSaved(key, input.value));
    }
    const saveBtn = el('button', {
      className: 'srw-btn', textContent: 'Save',
      title: 'Saves the URL and the API key together',
      onClick: async (e) => {
        e.target.disabled = true;
        try {
          const changed = await savePending();
          status.textContent = changed.length ? ' saved' : ' nothing changed';
          setTimeout(() => { status.textContent = ''; }, 2000);
          if (changed.length) window.SRW.notify((check && check.name) || 'Connection', 'Saved.', 'success');
          notify(changed);
        } catch (err) { status.textContent = ` error: ${err.message}`; } finally { e.target.disabled = false; }
      },
    });
    section.appendChild(saveBtn);
    if (check) {
      section.appendChild(el('button', {
        className: 'srw-btn', textContent: 'Check connection',
        title: `Saves what you typed, then checks that ${check.name} answers (nothing else is changed)`,
        onClick: async (e) => {
          e.target.disabled = true;
          try {
            status.textContent = ' saving…';
            const changed = await savePending();
            status.textContent = ' checking…';
            const result = await window.SRW.runOp('connections_check', { only: check.key });
            const r = result && (result.error ? { ok: false, error: result.error } : result[check.key]);
            const text = !r ? 'no result' : (r.ok ? `${changed.length ? 'Saved. ' : ''}${check.name} answers.` : `${changed.length ? 'Saved, but ' : ''}${check.name} does not answer: ${r.error}`);
            status.textContent = !r ? ' error: no result' : (r.ok ? `${changed.length ? ' saved, ' : ' '}ok` : ` error: ${r.error}`);
            // a toast as well: saving re-renders the connection list, which would wipe the line above
            window.SRW.notify(check.name, text, r && r.ok ? 'success' : 'error');
            notify(changed);
          } catch (err) { status.textContent = ` error: ${err.message}`; } finally { e.target.disabled = false; }
        },
      }));
    }
    section.appendChild(status);
    section.appendChild(el('br'));
  }

  // Each connection's own settings/sync jobs now live inside that
  // connection's own box instead of scattered across separate top-
  // level sections (2026-09-12, user's own reorganization request --
  // by the time there were four connections plus five more settings
  // sections, it wasn't obvious which setting belonged to which
  // service without reading every heading).
  async function buildStashDBConnectionSection(settings, effective, secretPlaceholder, onFieldSaved) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'StashDB' }),
    ]);
    buildConnectionPair(section, {
      url: { key: 'stashdb_url', value: settings.stashdb_url, placeholder: effective.stashdb_url || 'https://stashdb.org/graphql' },
      apiKey: { key: 'stashdb_api_key', value: settings.stashdb_api_key, placeholder: secretPlaceholder(effective.stashdb_api_key_set) },
      check: { name: 'StashDB', key: 'stashdb' }, onSaved: onFieldSaved,
    });

    // No officially published StashDB.org rate limit is known (checked
    // 2026-09-11) -- the only documented comparable figure is a sibling
    // stash-box instance (ThePornDB) enforcing 240 req/min. This delay
    // is between each per-entity StashDB query during a scan.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Scan' }));
    const delayInput = el('input', {
      type: 'number', min: '0', step: '0.1', value: settings.stashdb_query_delay || '0.3',
    });
    section.appendChild(el('label', {}, ['Query delay (seconds between requests): ', delayInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'stashdb_query_delay', value: delayInput.value });
      },
    }));

    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'StashDB.org favorites' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Imports your own StashDB.org account favorites (Performers/Studios you starred '
        + 'there) into this Stash instance’s local Favorite flag, for whichever of them already exist '
        + 'here — one-directional, never removes a local favorite.',
    }));
    const favIntervalInput = el('input', {
      type: 'number', min: '1', step: '1',
      value: settings.stashdb_favorites_sync_interval_hours || '24',
    });
    section.appendChild(el('label', {}, ['Sync interval (hours): ', favIntervalInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', {
          key: 'stashdb_favorites_sync_interval_hours', value: favIntervalInput.value,
        });
      },
    }));
    const favSyncStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('br'));
    section.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Sync now',
      onClick: async (e) => {
        e.target.disabled = true;
        favSyncStatus.textContent = ' syncing...';
        try {
          const result = await window.SRW.runOp('stashdb_favorites_sync');
          if (result.error) {
            favSyncStatus.textContent = ` error: ${result.error}`;
          } else if (result.skipped) {
            favSyncStatus.textContent = ` ${result.reason}`;
          } else {
            favSyncStatus.textContent = ` done — ${result.studios_favorited} studio(s), `
              + `${result.performers_favorited} performer(s) favorited locally`;
          }
        } catch (err) {
          favSyncStatus.textContent = ` error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    section.appendChild(favSyncStatus);

    return section;
  }

  // Whisparr supports fully custom quality profiles (this instance has
  // its own "VR" one, id 7, alongside the builtin ones) -- fetched live
  // for the dropdowns below rather than hardcoding a fixed id list,
  // since there's no way to guess what a given Whisparr install's
  // profiles are named/numbered. Added 2026-09-12: normal-content
  // profiles (e.g. "HD - 720p/1080p") pick the wrong resolution/tag set
  // for VR files, which need their own profile. See db.py's
  // `_migrate_release_is_vr()` for how a release gets flagged as VR in
  // the first place (StashDB's own "Virtual Reality" tag; a studio-name
  // fallback for ThePornDB, which has no equivalent tag).
  async function buildWhisparrConnectionSection(settings, effective, secretPlaceholder, onFieldSaved) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Whisparr' }),
    ]);
    buildConnectionPair(section, {
      url: { key: 'whisparr_url', value: settings.whisparr_url, placeholder: effective.whisparr_url || 'http://localhost:6969' },
      apiKey: { key: 'whisparr_api_key', value: settings.whisparr_api_key, placeholder: secretPlaceholder(effective.whisparr_api_key_set) },
      check: { name: 'Whisparr', key: 'whisparr' }, onSaved: onFieldSaved,
    });

    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Quality profiles' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Which Whisparr quality profile a hand-off uses. A release flagged as VR uses '
        + 'the second one if set, otherwise falls back to the first (normal) one.',
    }));
    let profiles = [];
    let profilesError = null;
    try {
      const result = await window.SRW.runOp('whisparr_quality_profiles');
      if (result && result.error) profilesError = result.error;
      else if (Array.isArray(result)) profiles = result;
      else profilesError = 'unexpected response';
    } catch (e) {
      profilesError = e.message;
    }
    function buildProfileField(label, key) {
      if (profilesError) {
        section.appendChild(el('p', {
          className: 'srw-hint',
          textContent: `${label}: couldn't load Whisparr's quality profiles (${profilesError}) -- `
            + 'check the URL/API key above and reload this page.',
        }));
        return;
      }
      const select = el('select', {}, [
        el('option', { value: '', textContent: '(not set)' }),
        ...profiles.map((p) => el('option', { value: String(p.id), textContent: `${p.name} (#${p.id})` })),
      ]);
      select.value = settings[key] || '';
      const status = el('span', { className: 'srw-key-status' });
      section.appendChild(el('label', {}, [`${label}: `, select]));
      section.appendChild(el('button', {
        className: 'srw-btn', textContent: 'Save',
        onClick: async () => {
          await window.SRW.runOp('settings_set', { key, value: select.value });
          status.textContent = ' saved';
          setTimeout(() => { status.textContent = ''; }, 2000);
        },
      }));
      section.appendChild(status);
      section.appendChild(el('br'));
    }
    buildProfileField('Normal', 'whisparr_quality_profile_id');
    buildProfileField('VR', 'whisparr_vr_quality_profile_id');

    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Safety sweep' }));
    const sweepInput = el('input', { type: 'number', min: '1', step: '1', value: settings.safety_sweep_interval_hours || '24' });
    section.appendChild(el('label', {}, ['Interval (hours): ', sweepInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'safety_sweep_interval_hours', value: sweepInput.value });
      },
    }));

    // Blocklist sync (2026-09-23) -- see whisparr_blocklist_sync.py's
    // own module docstring for why this is a periodic pull rather than
    // a live lookup: this project's blocklist knowledge needs to
    // already live in this sidecar's own DB before Whisparr is ever
    // decommissioned (the long-term goal), not be re-derived from
    // Whisparr on demand.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Blocklist sync' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Pulls every release Whisparr itself has blocklisted (automatically or via its own UI) '
        + 'into this sidecar’s own list, so this sidecar’s own search won’t grab the same wrong release '
        + 'again — even under a preset where this sidecar does the searching, not Whisparr.',
    }));
    const blocklistIntervalInput = el('input', {
      type: 'number', min: '1', step: '1',
      value: settings.whisparr_blocklist_sync_interval_hours || '24',
    });
    section.appendChild(el('label', {}, ['Sync interval (hours): ', blocklistIntervalInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', {
          key: 'whisparr_blocklist_sync_interval_hours', value: blocklistIntervalInput.value,
        });
      },
    }));
    const blocklistSyncStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('br'));
    section.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Sync now',
      onClick: async (e) => {
        e.target.disabled = true;
        blocklistSyncStatus.textContent = ' syncing...';
        try {
          const result = await window.SRW.runOp('whisparr_blocklist_sync');
          if (result.error) {
            blocklistSyncStatus.textContent = ` error: ${result.error}`;
          } else if (result.skipped) {
            blocklistSyncStatus.textContent = ` ${result.reason}`;
          } else {
            blocklistSyncStatus.textContent = ` done — ${result.synced} of ${result.total} entries synced`;
          }
        } catch (err) {
          blocklistSyncStatus.textContent = ` error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    section.appendChild(blocklistSyncStatus);

    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Import status' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Sending a release to Whisparr only starts the download/import there — this checks '
        + 'back on releases still marked "sent" and flips them to "imported" once Whisparr actually has '
        + 'the file.',
    }));
    const checkIntervalInput = el('input', {
      type: 'number', min: '0.1', step: '0.1',
      value: settings.import_check_interval_hours || '1',
    });
    section.appendChild(el('label', {}, ['Check interval (hours): ', checkIntervalInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'import_check_interval_hours', value: checkIntervalInput.value });
      },
    }));
    const checkStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('br'));
    section.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Check now',
      onClick: async (e) => {
        e.target.disabled = true;
        checkStatus.textContent = ' checking...';
        try {
          const result = await window.SRW.runOp('import_check_run');
          if (result.error) {
            checkStatus.textContent = ` error: ${result.error}`;
          } else if (result.skipped) {
            checkStatus.textContent = ` ${result.reason}`;
          } else {
            checkStatus.textContent = ` done — ${result.imported.length} of ${result.checked} now imported, `
              + `${result.still_pending} still pending`;
          }
        } catch (err) {
          checkStatus.textContent = ` error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    section.appendChild(checkStatus);

    return section;
  }

  function buildThePornDBConnectionSection(settings, effective, secretPlaceholder, onFieldSaved) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'ThePornDB' }),
    ]);
    buildConnectionPair(section, {
      url: { key: 'theporndb_url', value: settings.theporndb_url, placeholder: effective.theporndb_url || 'https://api.theporndb.net' },
      apiKey: { key: 'theporndb_api_key', value: settings.theporndb_api_key, placeholder: secretPlaceholder(effective.theporndb_api_key_set) },
      check: { name: 'ThePornDB', key: 'theporndb' }, onSaved: onFieldSaved,
    });

    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'ThePornDB identify' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Matches your currently-favorited Studios/Performers against ThePornDB (same '
        + 'mechanism as Stash’s own Tagger), so the ThePornDB scan has something to find. Only sets '
        + 'the ID link — never overwrites existing names, images, or other details. Already-matched '
        + 'entities are left alone, so this is cheap to re-run.',
    }));
    const intervalInput = el('input', {
      type: 'number', min: '1', step: '1',
      value: settings.theporndb_identify_interval_hours || '24',
    });
    section.appendChild(el('label', {}, ['Sync interval (hours): ', intervalInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', {
          key: 'theporndb_identify_interval_hours', value: intervalInput.value,
        });
      },
    }));

    // Stash's own batch-tag mutation has no request-pacing parameter of
    // its own -- this delay (between each 20-entity batch this project
    // submits) is the only lever to stay under ThePornDB's stash-box
    // rate limit (240 req/min, the only documented figure found this
    // session, checked 2026-09-11 -- see PROGRESS.md).
    const delayInput = el('input', {
      type: 'number', min: '0', step: '0.5',
      value: settings.theporndb_identify_delay_seconds || '3.0',
    });
    section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['Delay between batches (seconds): ', delayInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', {
          key: 'theporndb_identify_delay_seconds', value: delayInput.value,
        });
      },
    }));

    const syncStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('br'));
    section.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Sync now',
      onClick: async (e) => {
        e.target.disabled = true;
        syncStatus.textContent = ' matching... (can take a while on a large watchlist)';
        try {
          const result = await window.SRW.runOp('theporndb_identify_sync');
          if (result.error) {
            syncStatus.textContent = ` error: ${result.error}`;
          } else if (result.skipped) {
            syncStatus.textContent = ` ${result.reason}`;
          } else {
            let text = ` done — ${result.studios_matched} studio(s), `
              + `${result.performers_matched} performer(s) newly matched`;
            if (result.errors && result.errors.length) {
              text += ` (${result.errors.length} error(s), see server logs)`;
            }
            syncStatus.textContent = text;
          }
        } catch (err) {
          syncStatus.textContent = ` error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    section.appendChild(syncStatus);

    return section;
  }

  // Prowlarr (2026-09-20) -- first real Connections-tab field for it,
  // replacing the .env-only PROWLARR_URL/PROWLARR_API_KEY used during
  // the exploratory live investigation (see project_roadmap_todo.md
  // memory / PROGRESS.md). Deliberately just connection config + a
  // reachability check -- NOT wired into indexer_search.py's own
  // candidate selection yet, that's its own separate, not-yet-designed
  // feature.
  function buildProwlarrConnectionSection(settings, effective, secretPlaceholder, onFieldSaved) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Prowlarr' }),
    ]);
    buildConnectionPair(section, {
      url: { key: 'prowlarr_url', value: settings.prowlarr_url, placeholder: effective.prowlarr_url || 'http://localhost:9696' },
      apiKey: { key: 'prowlarr_api_key', value: settings.prowlarr_api_key, placeholder: secretPlaceholder(effective.prowlarr_api_key_set) },
      check: { name: 'Prowlarr', key: 'prowlarr' }, onSaved: onFieldSaved,
    });

    return section;
  }

  // "Check connection" button of one service (Prowlarr, Whisparr, SABnzbd, a metadata provider): asks the sidecar to ping just that
  // one, with the URL/key as saved. The Release Manager page header only shows Stash and the sidecar; everything else is checked here.
  function buildCheckConnection(section, name, key) {
    const { el } = window.SRW;
    const checkStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Check connection',
      title: `Checks that ${name} answers with the saved URL and API key (nothing is changed)`,
      onClick: async (e) => {
        e.target.disabled = true;
        checkStatus.textContent = ' checking...';
        try {
          const result = await window.SRW.runOp('connections_check', { only: key });
          const r = result && (result.error ? { ok: false, error: result.error } : result[key]);
          if (!r) checkStatus.textContent = ' error: no result';
          else checkStatus.textContent = r.ok ? ' ok' : ` error: ${r.error}`;
        } catch (err) {
          checkStatus.textContent = ` error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    section.appendChild(checkStatus);
  }

  // ARR-style Connections list (2026-09-20 redesign) -- replaces the old
  // flat stack of always-visible per-provider boxes with a managed list
  // (see core.js's buildManagedList()): only a TYPE with something
  // actually configured shows its box, everything else is reachable via
  // an "Add connection" picker. Stash itself is deliberately NOT one of
  // these types -- see buildConnectionField's own comment above for why
  // it is handled in the setup guide's Stash step instead.
  //
  // `settingsKeys` is what "Remove" clears -- only the two fields that
  // decide `isConfigured`, never a type's other settings (sync
  // intervals, quality-profile assignments, ...), so removing and
  // re-adding a connection doesn't also wipe unrelated configuration.
  // Removing a connection here is a pure settings/visibility action --
  // it does NOT change any hand-off/search behavior (e.g. removing
  // Whisparr here doesn't stop it being used -- that's the separate,
  // not-yet-built "Whisparr-scope Settings" item).
  // StashDB/ThePornDB moved out to their own "Metadata Providers" tab
  // (2026-09-21, see BUILTIN_METADATA_PROVIDER_TYPES/
  // buildMetadataProvidersSection() below) -- they're metadata/discovery
  // sources, conceptually distinct from Whisparr/Prowlarr here, which
  // are hand-off/search backends.
  const CONNECTION_TYPES = [
    {
      key: 'whisparr', label: 'Whisparr', settingsKeys: ['whisparr_url', 'whisparr_api_key'],
      isConfigured: (effective) => !!effective.whisparr_url,
      build: buildWhisparrConnectionSection,
    },
    {
      key: 'prowlarr', label: 'Prowlarr', settingsKeys: ['prowlarr_url', 'prowlarr_api_key'],
      isConfigured: (effective) => !!effective.prowlarr_url,
      build: buildProwlarrConnectionSection,
    },
  ];

  async function buildConnectionsSection(settings, effective, secretPlaceholder) {
    const { el, buildManagedList, confirmInline } = window.SRW;
    const section = el('div');

    // Both `settings`/`effective` were fetched once up front in render()
    // (see its own comment on why) -- kept in sync here by mutating them
    // directly after a save/remove instead of re-fetching, same pattern
    // buildQualityProfilesSection's VR checkbox already used.
    function updateLocalState(key, value) {
      settings[key] = value;
      if (key.endsWith('_url')) effective[key] = value;
      else if (key.endsWith('_api_key')) effective[`${key}_set`] = !!value;
    }

    function onFieldSaved(key, value) {
      updateLocalState(key, value);
      refresh();
    }

    async function buildEntry(type) {
      const box = await type.build(settings, effective, secretPlaceholder, onFieldSaved);
      const removeStatus = el('span', { className: 'srw-key-status' });
      box.appendChild(el('button', {
        className: 'srw-btn srw-btn-danger', textContent: 'Remove connection',
        onClick: (e) => {
          confirmInline(e.currentTarget, `Remove the ${type.label} connection?`, async () => {
            for (const key of type.settingsKeys) {
              await window.SRW.runOp('settings_set', { key, value: '' });
              settings[key] = '';
            }
            // Re-fetch the real server-resolved state instead of
            // assuming "cleared here" == "not configured" -- this
            // project's established override-falls-back-to-env pattern
            // (get_setting_value()) means a value also present in
            // api/.env keeps the connection "configured" from the
            // sidecar's own point of view even after this page's own
            // override is cleared, exactly what a page reload would
            // also show. Surfacing that immediately (rather than
            // showing "removed" only for it to silently reappear next
            // reload) avoids a misleading UI state -- confirmed live
            // 2026-09-20 this actually happens for every connection in
            // a setup where every value originally came from .env.
            const fresh = await window.SRW.runOp('settings_get_effective_connections');
            Object.assign(effective, fresh);
            if (type.isConfigured(effective)) {
              removeStatus.textContent = ' cleared here, but still active via api/.env -- edit .env directly to fully remove it';
              setTimeout(refresh, 4000);
            } else {
              refresh();
            }
          }, { danger: true });
        },
      }));
      box.appendChild(removeStatus);
      return box;
    }

    async function buildAdder() {
      const unconfigured = CONNECTION_TYPES.filter((t) => !t.isConfigured(effective));
      if (unconfigured.length === 0) return null;
      const wrap = el('div', { className: 'srw-settings-section' });
      const select = el('select', {}, unconfigured.map((t) => el('option', { value: t.key, textContent: t.label })));
      wrap.appendChild(el('label', {}, ['Add connection: ', select]));
      wrap.appendChild(el('button', {
        className: 'srw-btn srw-btn-primary', textContent: 'Add',
        onClick: async () => {
          const type = unconfigured.find((t) => t.key === select.value);
          wrap.replaceWith(await buildEntry(type));
        },
      }));
      return wrap;
    }

    const { refresh } = buildManagedList(section, {
      emptyMessage: 'No connections configured yet.',
      fetchItems: async () => CONNECTION_TYPES.filter((t) => t.isConfigured(effective)),
      buildEntry: (type) => buildEntry(type),
      buildAdder: () => buildAdder(),
    });
    await refresh();
    return section;
  }

  // The stash-boxes configured in Stash (Settings > Metadata Providers > Stash-box endpoints), v0.186.0. StashDB's and ThePornDB's keys are
  // taken over automatically while the sidecar has none; every other stash-box is offered here and added as a provider on request (each
  // provider is queried on every scan). `getReport()` is the last `stash_sync` report (no keys in it), `sync(args)` runs it again
  // ({add: [endpoint, ...]} adds, {overwrite: true} also replaces keys already set), `onChange()` lets the caller refresh its own view.
  function buildStashBoxImport({ getReport, sync, onChange }) {
    const { el, confirmInline } = window.SRW;
    const wrap = el('div', { className: 'srw-settings-section' });
    const status = el('span', { className: 'srw-key-status' });
    const selected = new Set();
    const BUILTIN_LABEL = { stashdb: 'StashDB', theporndb: 'ThePornDB' };
    const STATUS_TEXT = {
      filled: 'key taken over from Stash', updated: 'key updated from Stash', added: 'added', present: 'set up',
      available: 'not added yet', skipped: 'no API key in Stash',
    };

    async function run(args, busyText) {
      status.textContent = ` ${busyText}...`;
      try {
        const r = await sync(args);
        status.textContent = r && r.error ? ` error: ${r.error}` : '';
      } catch (err) {
        status.textContent = ` error: ${err.message}`;
      }
      selected.clear();
      paint();
      if (onChange) await onChange();
    }

    function paint() {
      const report = getReport();
      const head = el('h4', { textContent: 'Stash-boxes in your Stash' });
      if (!report || report.error) {
        wrap.replaceChildren(head, el('p', { className: 'srw-hint', textContent: `Could not ask the sidecar: ${(report && report.error) || 'no answer'}` }));
        return;
      }
      if (report.discovered === false) {
        wrap.replaceChildren(head, el('p', {
          className: 'srw-hint',
          textContent: 'Stash’s configuration could not be read from here. The keys can still be entered by hand.',
        }));
        return;
      }
      const boxes = report.boxes || [];
      if (!boxes.length) {
        wrap.replaceChildren(head, el('p', {
          className: 'srw-hint', textContent: 'No stash-box endpoints are configured in Stash (Stash > Settings > Metadata Providers).',
        }));
        return;
      }
      const addable = boxes.filter((b) => b.status === 'available');
      const rows = boxes.map((b) => {
        const builtin = BUILTIN_LABEL[b.kind];
        const label = !builtin ? b.name
          : (b.name.toLowerCase() === builtin.toLowerCase() ? `${b.name} (built in)` : `${b.name} (${builtin}, built in)`);
        const text = el('span', {}, [`${label} — `, el('span', { className: 'srw-hint', textContent: b.endpoint }), ` — ${STATUS_TEXT[b.status] || b.status}`]);
        if (b.status !== 'available') return el('li', { className: 'srw-release-item' }, [text]);
        const box = el('input', { type: 'checkbox' });
        box.addEventListener('change', () => {
          if (box.checked) selected.add(b.endpoint); else selected.delete(b.endpoint);
          addBtn.disabled = selected.size === 0;
        });
        return el('li', { className: 'srw-release-item' }, [el('label', {}, [box, ' ', text])]);
      });
      const addBtn = el('button', {
        className: 'srw-btn srw-btn-primary', textContent: 'Add selected as providers', disabled: true,
        title: 'Each added provider is queried on every scan.',
        onClick: () => run({ add: [...selected] }, 'adding'),
      });
      const updateBtn = el('button', {
        className: 'srw-btn', textContent: 'Update keys from Stash',
        title: 'Replaces the API keys set here with the ones Stash holds (for the endpoints listed above).',
        onClick: (e) => confirmInline(e.currentTarget, 'Replace the keys set here with the ones from Stash?', () => run({ overwrite: true }, 'updating')),
      });
      const readBtn = el('button', { className: 'srw-btn', textContent: 'Read Stash again', onClick: () => run({}, 'reading') });
      wrap.replaceChildren(
        head,
        el('p', {
          className: 'srw-hint',
          textContent: 'Read from your Stash, keys included, so nothing needs to be copied. StashDB and ThePornDB are filled in by themselves; '
            + (addable.length ? 'tick the other ones you want this program to watch.' : 'there are no further ones to add.'),
        }),
        el('ul', { className: 'srw-release-list' }, rows),
        el('div', { className: 'srw-toolbar' }, [addable.length ? addBtn : null, updateBtn, readBtn, status]),
      );
    }

    paint();
    return wrap;
  }

  // Metadata Providers tab (2026-09-21, Stage B of the Metadata
  // Provider generalization -- see api/metadata_provider_repo.py for
  // the backend half). StashDB/ThePornDB are "builtin" types (own
  // settings keys, own field builders, same Remove-clears-the-two-
  // fields semantics as CONNECTION_TYPES above) shown/added exactly
  // like Whisparr/Prowlarr are on the Connections tab. A genuinely NEW
  // provider is a real DB row (`metadata_provider` table) instead --
  // its own name/URL/API-key, editable, and actually deletable (not
  // just "clear the two fields back to blank"), since unlike
  // StashDB/ThePornDB there's no built-in settings key for it to fall
  // back to. Both kinds share one list via buildManagedList(), each
  // `fetchItems()` entry tagged with which kind it is so buildEntry()
  // knows which box builder to use.
  const BUILTIN_METADATA_PROVIDER_TYPES = [
    {
      key: 'stashdb', label: 'StashDB', settingsKeys: ['stashdb_url', 'stashdb_api_key'],
      isConfigured: (effective) => !!effective.stashdb_url,
      build: buildStashDBConnectionSection,
    },
    {
      key: 'theporndb', label: 'ThePornDB', settingsKeys: ['theporndb_url', 'theporndb_api_key'],
      isConfigured: (effective) => !!effective.theporndb_url,
      build: buildThePornDBConnectionSection,
    },
  ];

  // Colour of a provider in lists, badges and the calendar (v0.152.0): one colour picker, a live preview badge, Save and "Reset" (the default /
  // automatic colour). The built-in providers keep it in a setting, an added stash-box in its own row.
  function buildProviderColorField(section, key) {
    const { el } = window.SRW;
    const info = window.SRW.providerInfo(key);
    const input = el('input', { type: 'color' });
    input.value = info.color;
    const preview = el('span', { className: `srw-provider-badge srw-provider-badge-${key}`, textContent: info.name });
    const status = el('span', { className: 'srw-key-status' });
    async function store(value) {
      const result = info.builtin
        ? await window.SRW.runOp('settings_set', { key: `provider_color_${key}`, value })
        : await window.SRW.runOp('metadata_providers_update', { key, color: value });
      if (result && result.error) { status.textContent = ` error: ${result.error}`; return; }
      await window.SRW.resetProviders();
      input.value = window.SRW.providerInfo(key).color;
      status.textContent = ' saved';
      setTimeout(() => { status.textContent = ''; }, 2000);
    }
    section.appendChild(el('div', {}, [
      el('label', {}, ['Colour in lists and calendar: ', input]), ' ', preview, ' ',
      el('button', { type: 'button', className: 'srw-btn', textContent: 'Save', onClick: () => store(input.value) }),
      el('button', { type: 'button', className: 'srw-btn', textContent: 'Reset', title: 'Back to the default colour', onClick: () => store('') }),
      status,
    ]));
  }

  function buildGenericProviderEntry(provider, refresh) {
    const { el, confirmInline } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: `${provider.name} (${provider.key})` }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Any stash-box GraphQL endpoint (same protocol as StashDB) -- confirmed live to also '
          + 'work with FansDB, PMV Stash, and JAVStash.',
      }),
    ]);
    const nameInput = el('input', { type: 'text', value: provider.name });
    const urlInput = el('input', { type: 'text', value: provider.url });
    const apiKeyInput = el('input', { type: 'password', autocomplete: 'off', value: provider.api_key });
    const saveStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {}, ['Name: ', nameInput])); section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['URL: ', urlInput])); section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['API key: ', apiKeyInput])); section.appendChild(el('br'));
    const delayInput = el('input', {
      type: 'number', min: '0', step: '0.1', value: provider.query_delay == null ? '' : String(provider.query_delay),
      placeholder: 'default (as StashDB)',
    });
    section.appendChild(el('label', {}, ['Query delay (seconds between requests): ', delayInput]));
    const favInput = el('input', {
      type: 'number', min: '1', step: '1', value: provider.favorites_interval == null ? '' : String(provider.favorites_interval),
      placeholder: 'default (as StashDB)',
    });
    section.appendChild(el('label', {}, ['Favorites sync interval (hours): ', favInput]));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'We do not know this box\'s limits yet, so empty fields use the StashDB values (delay and favorites sync interval). Raise the delay if the box '
        + 'rate-limits you. The favorites sync marks the account favorites of this box as Favorite in Stash (one direction, never removes one).',
    }));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        try {
          // A validation failure (e.g. a duplicate/reserved key) comes
          // back as a plain {"error": ...} value here, not a thrown
          // exception -- runOp()'s underlying transport (the plugin's
          // own runPluginOperation GraphQL mutation) always succeeds
          // regardless of what the sidecar's own HTTP call inside it
          // returned, so `result.error` has to be checked explicitly.
          const result = await window.SRW.runOp('metadata_providers_update', {
            key: provider.key, name: nameInput.value, url: urlInput.value, api_key: apiKeyInput.value,
            query_delay: delayInput.value, favorites_interval: favInput.value,
          });
          if (result && result.error) {
            saveStatus.textContent = ` error: ${result.error}`;
          } else {
            saveStatus.textContent = ' saved';
            setTimeout(() => { saveStatus.textContent = ''; }, 2000);
          }
        } catch (err) {
          saveStatus.textContent = ` error: ${err.message}`;
        }
      },
    }));
    section.appendChild(saveStatus);

    buildCheckConnection(section, provider.name, provider.key);
    buildProviderColorField(section, provider.key);

    section.appendChild(el('button', {
      className: 'srw-btn srw-btn-danger', textContent: 'Remove connection',
      onClick: (e) => {
        confirmInline(e.currentTarget, `Remove the ${provider.name} provider entirely?`, async () => {
          await window.SRW.runOp('metadata_providers_delete', { key: provider.key });
          refresh();
        }, { danger: true });
      },
    }));
    return section;
  }

  function buildNewGenericProviderForm(refresh) {
    const { el } = window.SRW;
    const wrap = el('div', { className: 'srw-settings-section' });
    const keyInput = el('input', {
      type: 'text', placeholder: 'e.g. fansdb (lowercase, no spaces)',
    });
    const nameInput = el('input', { type: 'text', placeholder: 'e.g. FansDB' });
    const urlInput = el('input', { type: 'text', placeholder: 'https://fansdb.cc/graphql' });
    const apiKeyInput = el('input', { type: 'password', autocomplete: 'off', placeholder: 'API key' });
    const status = el('span', { className: 'srw-key-status' });
    wrap.appendChild(el('h4', { textContent: 'Add a new stash-box provider' }));
    wrap.appendChild(el('label', {}, ['Key: ', keyInput])); wrap.appendChild(el('br'));
    wrap.appendChild(el('label', {}, ['Name: ', nameInput])); wrap.appendChild(el('br'));
    wrap.appendChild(el('label', {}, ['URL: ', urlInput])); wrap.appendChild(el('br'));
    wrap.appendChild(el('label', {}, ['API key: ', apiKeyInput])); wrap.appendChild(el('br'));
    wrap.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Add provider',
      onClick: async () => {
        if (!keyInput.value || !nameInput.value || !urlInput.value) {
          status.textContent = ' key, name, and URL are required';
          return;
        }
        try {
          // See buildGenericProviderEntry()'s Save handler for why
          // result.error (not just a thrown exception) is checked here.
          const result = await window.SRW.runOp('metadata_providers_create', {
            key: keyInput.value, name: nameInput.value, url: urlInput.value, api_key: apiKeyInput.value,
          });
          if (result && result.error) {
            status.textContent = ` error: ${result.error}`;
          } else {
            refresh();
          }
        } catch (err) {
          status.textContent = ` error: ${err.message}`;
        }
      },
    }));
    wrap.appendChild(status);
    return wrap;
  }

  async function buildMetadataProvidersSection(settings, effective, secretPlaceholder) {
    const { el, buildManagedList, confirmInline } = window.SRW;
    const section = el('div');

    function updateLocalState(key, value) {
      settings[key] = value;
      if (key.endsWith('_url')) effective[key] = value;
      else if (key.endsWith('_api_key')) effective[`${key}_set`] = !!value;
    }

    function onFieldSaved(key, value) {
      updateLocalState(key, value);
      refresh();
    }

    async function buildBuiltinEntry(type) {
      const box = await type.build(settings, effective, secretPlaceholder, onFieldSaved);
      buildProviderColorField(box, type.key);
      const removeStatus = el('span', { className: 'srw-key-status' });
      box.appendChild(el('button', {
        className: 'srw-btn srw-btn-danger', textContent: 'Remove connection',
        onClick: (e) => {
          confirmInline(e.currentTarget, `Remove the ${type.label} connection?`, async () => {
            for (const key of type.settingsKeys) {
              await window.SRW.runOp('settings_set', { key, value: '' });
              settings[key] = '';
            }
            // Same "re-fetch the real state, don't assume" reasoning as
            // buildConnectionsSection()'s own Remove handler -- a value
            // also present in api/.env keeps this "configured" even
            // after clearing this page's own override.
            const fresh = await window.SRW.runOp('settings_get_effective_connections');
            Object.assign(effective, fresh);
            if (type.isConfigured(effective)) {
              removeStatus.textContent = ' cleared here, but still active via api/.env -- edit .env directly to fully remove it';
              setTimeout(refresh, 4000);
            } else {
              refresh();
            }
          }, { danger: true });
        },
      }));
      box.appendChild(removeStatus);
      return box;
    }

    async function buildEntry(item) {
      return item.kind === 'builtin' ? buildBuiltinEntry(item.type) : buildGenericProviderEntry(item.provider, refresh);
    }

    async function buildAdder() {
      const unconfiguredBuiltins = BUILTIN_METADATA_PROVIDER_TYPES.filter((t) => !t.isConfigured(effective));
      if (unconfiguredBuiltins.length === 0) return buildNewGenericProviderForm(refresh);
      const wrap = el('div', { className: 'srw-settings-section' });
      const NEW_PROVIDER_VALUE = '__new__';
      const select = el('select', {}, [
        ...unconfiguredBuiltins.map((t) => el('option', { value: t.key, textContent: t.label })),
        el('option', { value: NEW_PROVIDER_VALUE, textContent: '+ Add a new stash-box provider...' }),
      ]);
      wrap.appendChild(el('label', {}, ['Add: ', select]));
      wrap.appendChild(el('button', {
        className: 'srw-btn srw-btn-primary', textContent: 'Add',
        onClick: async () => {
          if (select.value === NEW_PROVIDER_VALUE) {
            wrap.replaceWith(buildNewGenericProviderForm(refresh));
            return;
          }
          const type = unconfiguredBuiltins.find((t) => t.key === select.value);
          wrap.replaceWith(await buildBuiltinEntry(type));
        },
      }));
      return wrap;
    }

    const { refresh } = buildManagedList(section, {
      emptyMessage: 'No metadata providers configured yet.',
      fetchItems: async () => {
        const builtins = BUILTIN_METADATA_PROVIDER_TYPES
          .filter((t) => t.isConfigured(effective))
          .map((type) => ({ kind: 'builtin', type }));
        let generics = [];
        try {
          const result = await window.SRW.runOp('metadata_providers_list');
          if (Array.isArray(result)) generics = result.map((provider) => ({ kind: 'generic', provider }));
        } catch (e) { /* shown as empty */ }
        return [...builtins, ...generics];
      },
      buildEntry: (item) => buildEntry(item),
      buildAdder: () => buildAdder(),
    });
    await refresh();
    // Stash-boxes configured in Stash: offered below the list (v0.186.0).
    let stashReport = null;
    const syncFromStash = async (args) => { stashReport = await window.SRW.runOp('stash_sync', args || {}); return stashReport; };
    await syncFromStash();
    section.appendChild(buildStashBoxImport({
      getReport: () => stashReport,
      sync: syncFromStash,
      onChange: async () => {
        Object.assign(effective, await window.SRW.runOp('settings_get_effective_connections'));
        Object.assign(settings, await window.SRW.runOp('settings_get_all'));
        await window.SRW.resetProviders();
        refresh();
      },
    }));
    return section;
  }

  // Added 2026-09-19 while investigating a Milestone 2 completed
  // download that vanished from Whisparr's own `/queue` before this
  // project's file-import poll ever saw it (see the in-progress
  // cutover memory note) -- SABnzbd is the actual download client
  // behind Whisparr, and this project had no way to ask it directly
  // what happened to a job. Not used by any automated pipeline yet
  // (see download_sources.py's module docstring re: a future
  // Milestone 3 `SabnzbdSource`) -- today this is a connection plus a
  // manual diagnostic lookup only.
  // Milestone 3 (2026-09-19): replaces the old single SABnzbd
  // connection box -- indexer_search.py can send a chosen release to
  // ANY configured, enabled download client matching the release's
  // protocol (lowest priority-number wins), not just one hardcoded
  // SABnzbd connection. Only 'sabnzbd' is a real client type today; a
  // torrent client type can be added later without a schema change,
  // per the user's explicit request to keep this extensible without
  // building a second real client right now. An existing install's old
  // sabnzbd_url/sabnzbd_api_key settings are migrated into one seeded
  // row here automatically (see db.py) -- nothing to re-enter on
  // upgrade.
  async function buildDownloadClientsSection(onChanged) {
    const { el, buildManagedList } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Download Clients' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Where indexer_search.py sends a chosen release. The lowest-priority-number '
          + 'enabled client matching a release\'s protocol (usenet/torrent) wins.',
      }),
    ]);

    const { refresh } = buildManagedList(section, {
      emptyMessage: 'No download clients configured yet.',
      fetchItems: async () => {
        const result = await window.SRW.runOp('download_clients_list');
        return Array.isArray(result) ? result : [];
      },
      buildEntry: (client) => buildClientRow(client),
      buildAdder: () => buildClientForm(null),
    });

    function buildClientRow(client) {
      const status = el('span', { className: 'srw-key-status' });
      const row = el('div', { className: 'srw-dc-row' }, [
        el('strong', { textContent: `${client.name} ` }),
        el('span', { className: 'srw-hint', textContent: `(${client.type}, ${client.protocol}, priority ${client.priority}${client.enabled ? '' : ', disabled'}) ` }),
        el('button', {
          className: 'srw-btn', textContent: 'Check',
          onClick: async (e) => {
            e.target.disabled = true;
            status.textContent = ' checking...';
            try {
              const result = await window.SRW.runOp('download_clients_check', { id: client.id });
              status.textContent = result.error ? ` error: ${result.error}` : (result.version ? ` ok (qBittorrent ${result.version})` : ' ok');
            } catch (err) {
              status.textContent = ` error: ${err.message}`;
            } finally {
              e.target.disabled = false;
            }
          },
        }),
        client.type === 'sabnzbd' ? el('button', {
          className: 'srw-btn', textContent: 'Clean history',
          title: 'Remove the finished, fully imported downloads of this program from the client\'s history (asks first, counts before)',
          onClick: async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            status.textContent = ' counting...';
            const probe = await window.SRW.runOp('download_clients_clean_history', { id: client.id, dry_run: true });
            btn.disabled = false;
            if (probe.error) { status.textContent = ` error: ${probe.error}`; return; }
            status.textContent = '';
            if (!probe.found) { status.textContent = ' nothing to clean up'; return; }
            window.SRW.confirmInline(btn, `Remove ${probe.found} finished download(s) of this program from the history of "${client.name}"? (e.g. ${probe.sample.slice(0, 2).join(', ')}) The files are not touched.`, async () => {
              status.textContent = ' cleaning...';
              const res = await window.SRW.runOp('download_clients_clean_history', { id: client.id, dry_run: false });
              status.textContent = res.error ? ` error: ${res.error}` : ` ${res.removed} removed${res.errors && res.errors.length ? `, ${res.errors.length} failed (${res.errors[0]})` : ''}`;
            });
          },
        }) : null,
        el('button', {
          className: 'srw-btn', textContent: client.enabled ? 'Disable' : 'Enable',
          onClick: async () => {
            await window.SRW.runOp('download_clients_update', { id: client.id, ...client, enabled: !client.enabled });
            await refresh();
            if (onChanged) onChanged();
          },
        }),
        el('button', {
          className: 'srw-btn', textContent: 'Edit',
          onClick: () => {
            const form = buildClientForm(client);
            row.replaceWith(form);
          },
        }),
        el('button', {
          className: 'srw-btn srw-btn-danger', textContent: 'Delete',
          onClick: (e) => {
            window.SRW.confirmInline(e.currentTarget, `Delete download client "${client.name}"?`, async () => {
              await window.SRW.runOp('download_clients_delete', { id: client.id });
              await refresh();
              if (onChanged) onChanged();
            }, { danger: true });
          },
        }),
        status,
      ]);
      return row;
    }

    // Client form (v0.133.0): the type decides the protocol; qBittorrent carries the *arr-style client settings with their defaults
    // (category = app name, initial state Start, content layout Default, priorities Default, not sequential).
    function buildClientForm(existing) {
      const opts = { initial_state: 'start', recent_priority: 'default', older_priority: 'default', sequential: false, first_last: false,
        content_layout: 'default', post_import_category: '', ...((existing && existing.options) || {}) };
      const nameInput = el('input', { type: 'text', placeholder: 'Name', value: existing ? existing.name : '' });
      const typeSelect = el('select', {}, [
        el('option', { value: 'sabnzbd', textContent: 'SABnzbd (Usenet)' }),
        el('option', { value: 'qbittorrent', textContent: 'qBittorrent (Torrent)' }),
      ]);
      typeSelect.value = existing ? existing.type : 'sabnzbd';
      const priorityInput = el('input', {
        type: 'number', min: '1', step: '1', placeholder: 'Priority',
        value: existing ? existing.priority : '25',
      });
      const urlInput = el('input', { type: 'text', placeholder: 'URL (e.g. http://192.168.1.20:8080)', value: existing ? existing.url : '' });
      const apiKeyInput = el('input', {
        type: 'password', autocomplete: 'off', placeholder: 'API key',
        value: existing ? existing.api_key : '',
      });
      const userInput = el('input', { type: 'text', autocomplete: 'off', placeholder: 'User name', value: existing ? existing.username : '' });
      const passInput = el('input', { type: 'password', autocomplete: 'new-password', placeholder: 'Password', value: existing ? existing.password : '' });
      const categoryInput = el('input', {
        type: 'text', placeholder: 'Category (optional)', value: existing ? existing.category : '',
      });
      const select = (items, value) => { const sel = el('select', {}, items.map(([v, t]) => el('option', { value: v, textContent: t }))); sel.value = value; return sel; };
      const stateSel = select([['start', 'Start'], ['force_start', 'Force start'], ['pause', 'Pause']], opts.initial_state);
      const recentSel = select([['default', 'Default'], ['first', 'First'], ['last', 'Last']], opts.recent_priority);
      const olderSel = select([['default', 'Default'], ['first', 'First'], ['last', 'Last']], opts.older_priority);
      const layoutSel = select([['default', 'Default (qBittorrent setting)'], ['original', 'Original'], ['subfolder', 'Create subfolder']], opts.content_layout);
      const seqBox = el('input', { type: 'checkbox' }); seqBox.checked = !!opts.sequential;
      const flBox = el('input', { type: 'checkbox' }); flBox.checked = !!opts.first_last;
      const postCat = el('input', { type: 'text', placeholder: 'Post-import category (optional)', value: opts.post_import_category || '' });
      const remotePath = el('input', { type: 'text', placeholder: '/downloads', value: opts.remote_path || '' });
      const localPath = el('input', { type: 'text', placeholder: '/downloads', value: opts.local_path || '' });
      const formStatus = el('span', { className: 'srw-key-status' });
      const removeBox = el('input', { type: 'checkbox' }); removeBox.checked = opts.remove_completed !== false;
      const usenetPart = el('div', {}, [el('label', {}, ['API key: ', apiKeyInput]), el('br'),
        el('label', {}, [removeBox, ' Remove completed downloads from the history']),
        el('p', { className: 'srw-hint', textContent: 'Like the *arr applications: once a download is imported for good, its entry is removed from SABnzbd\'s history (only ones this program downloaded; the files are not touched). The release name is kept here, so the same release is never fetched twice.' })]);
      const torrentPart = el('div', {}, [
        el('label', {}, ['User name: ', userInput]), el('br'),
        el('label', {}, ['Password: ', passInput]), el('br'),
        el('label', {}, ['Initial state: ', stateSel]), el('br'),
        el('label', {}, ['Recent priority: ', recentSel]), el('label', {}, [' Older priority: ', olderSel]), el('br'),
        el('label', {}, ['Content layout: ', layoutSel]), el('br'),
        el('label', {}, [seqBox, ' Sequential order']), el('label', {}, [' ', flBox, ' First and last first']), el('br'),
        el('label', {}, ['Post-import category: ', postCat]), el('br'),
        el('label', {}, ['Download folder in qBittorrent: ', remotePath]), el('label', {}, [' as this program sees it: ', localPath]), el('br'),
        el('p', { className: 'srw-hint', textContent: 'Path mapping (like the *arr remote path mappings): the folder qBittorrent writes to, and the same folder in the sidecar container. Both empty = the SABnzbd downloads folder of Settings > File Import.' }),
        el('p', { className: 'srw-hint', textContent: 'Like the *arr applications: the downloaded files are hard-linked (else copied) into the library and the torrent keeps seeding; qBittorrent\'s own seeding limits (or the indexer\'s seed ratio / seed time) decide when it stops.' }),
      ]);
      const syncType = () => {
        const torrent = typeSelect.value === 'qbittorrent';
        usenetPart.hidden = torrent;
        torrentPart.hidden = !torrent;
        if (!existing && !categoryInput.value) categoryInput.placeholder = torrent ? 'Category (default: srm)' : 'Category (optional)';
      };
      typeSelect.addEventListener('change', syncType);
      syncType();

      // The form's values in one place, used by Add / Save and by the Test button.
      const collectBody = () => {
        const torrent = typeSelect.value === 'qbittorrent';
        return {
          name: nameInput.value, type: typeSelect.value, protocol: torrent ? 'torrent' : 'usenet',
          priority: parseInt(priorityInput.value, 10) || 25, url: urlInput.value.trim(),
          api_key: torrent ? '' : apiKeyInput.value, username: torrent ? userInput.value : '', password: torrent ? passInput.value : '',
          category: categoryInput.value || (torrent ? 'srm' : ''),
          options: torrent ? { initial_state: stateSel.value, recent_priority: recentSel.value, older_priority: olderSel.value,
            content_layout: layoutSel.value, sequential: seqBox.checked, first_last: flBox.checked, post_import_category: postCat.value,
            remote_path: remotePath.value.trim(), local_path: localPath.value.trim() } : { remove_completed: removeBox.checked },
          enabled: existing ? existing.enabled : true,
        };
      };

      // Prefill from Prowlarr's own download clients (Prowlarr hides the API key/password, so those are typed in).
      const prowlarrRow = el('div', { hidden: true });
      if (!existing) {
        window.SRW.runOp('download_clients_from_prowlarr').then((list) => {
          if (!Array.isArray(list) || !list.length) return;
          const pick = el('select', {}, list.map((c, i) => el('option', { value: String(i), textContent: `${c.name} (${c.type}, ${c.url})` })));
          const note = el('span', { className: 'srw-key-status' });
          prowlarrRow.appendChild(el('label', {}, ['Take over from Prowlarr: ', pick]));
          prowlarrRow.appendChild(el('button', {
            type: 'button', className: 'srw-btn', textContent: 'Prefill',
            onClick: () => {
              const c = list[Number(pick.value)];
              nameInput.value = c.name; typeSelect.value = c.type; syncType();
              urlInput.value = c.url; userInput.value = c.username || '';
              note.textContent = ' prefilled -- now enter the API key (or password): Prowlarr does not reveal it. Check that the address is reachable from this program (a container name from Prowlarr\'s network may not be).';
              (c.type === 'sabnzbd' ? apiKeyInput : passInput).focus();
            },
          }));
          prowlarrRow.appendChild(note);
          prowlarrRow.appendChild(el('br'));
          prowlarrRow.hidden = false;
        }).catch(() => {});
      }

      const form = el('div', { className: 'srw-dc-form' }, [
        prowlarrRow,
        el('label', {}, ['Name: ', nameInput]), el('br'),
        el('label', {}, ['Type: ', typeSelect]), el('br'),
        el('label', {}, ['Priority: ', priorityInput]), el('br'),
        el('label', {}, ['URL: ', urlInput]), el('br'),
        usenetPart, torrentPart,
        el('label', {}, ['Category: ', categoryInput]), el('br'),
        el('button', {
          className: 'srw-btn srw-btn-primary', textContent: existing ? 'Save' : 'Add client',
          onClick: async () => {
            const body = collectBody();
            if (!body.name || !body.url) {
              formStatus.textContent = ' name and URL are required';
              return;
            }
            try {
              const res = existing ? await window.SRW.runOp('download_clients_update', { id: existing.id, ...body })
                : await window.SRW.runOp('download_clients_create', body);
              if (res && res.error) { formStatus.textContent = ` error: ${res.error}`; return; }
              await refresh();
              if (onChanged) onChanged();
            } catch (err) {
              formStatus.textContent = ` error: ${err.message}`;
            }
          },
        }),
        // Checks what is typed into this form, saved or not (the per-client "Check" above only knows the saved clients).
        el('button', {
          className: 'srw-btn', textContent: 'Test connection', title: 'Checks the details entered above (nothing is saved)',
          onClick: async (e) => {
            const body = collectBody();
            if (!body.url) { formStatus.textContent = ' enter the URL first'; return; }
            e.target.disabled = true;
            formStatus.textContent = ' checking...';
            try {
              const r = await window.SRW.runOp('download_clients_test', {
                type: body.type, url: body.url, api_key: body.api_key, username: body.username, password: body.password,
              });
              formStatus.textContent = r && r.error ? ` error: ${r.error}` : (r && r.version ? ` ok (qBittorrent ${r.version})` : ' ok');
            } catch (err) {
              formStatus.textContent = ` error: ${err.message}`;
            } finally {
              e.target.disabled = false;
            }
          },
        }),
        formStatus,
      ]);
      return form;
    }

    await refresh();

    // Torrents (v0.135.0): the *arr "Remove Completed" option, off by default.
    const settingsNow = await window.SRW.runOp('settings_get_all').catch(() => ({}));
    const removeBox = el('input', { type: 'checkbox' });
    removeBox.checked = String((settingsNow && settingsNow.remove_completed_downloads) || 'false') === 'true';
    const removeStatus = el('span', { className: 'srw-key-status' });
    removeBox.addEventListener('change', async () => {
      const r = await window.SRW.runOp('settings_set', { key: 'remove_completed_downloads', value: removeBox.checked ? 'true' : 'false' });
      removeStatus.textContent = r && r.error ? ` error: ${r.error}` : ' saved';
    });
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Torrents: after seeding' }));
    section.appendChild(el('label', {}, [removeBox, ' Remove a finished torrent (with its files) from the client once its seeding goal is reached']));
    section.appendChild(removeStatus);
    section.appendChild(el('p', { className: 'srw-hint', textContent: 'Off by default, like the *arr applications. Only torrents this program grabbed and imported are touched, and only when qBittorrent itself reports the seed ratio / seed time limit as reached (torrent paused or stopped). The library keeps its own copy (hard link or copy).' }));

    // SABnzbd history lookup -- finds where a completed job actually
    // landed on disk (its "storage" path), for tracking down a
    // download that never made it into the import pipeline. Added
    // 2026-09-19 for the Milestone 2 cutover investigation, kept here
    // (against whichever usenet client the backend resolves, see
    // main.py's get_sabnzbd_client()) since it's still generically
    // useful.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'SABnzbd history lookup' }));
    const searchInput = el('input', { type: 'text', placeholder: 'Job name (optional, filters server-side)' });
    const resultsWrap = el('div');
    section.appendChild(el('label', {}, ['Search: ', searchInput]));
    section.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Look up',
      onClick: async (e) => {
        e.target.disabled = true;
        resultsWrap.textContent = ' looking up...';
        try {
          const rows = await window.SRW.runOp('sabnzbd_history', { search: searchInput.value || undefined });
          resultsWrap.innerHTML = '';
          if (rows && rows.error) {
            resultsWrap.textContent = `Error: ${rows.error}`;
          } else if (!Array.isArray(rows) || rows.length === 0) {
            resultsWrap.textContent = 'No matching history entries.';
          } else {
            const ul = el('ul', { className: 'srw-sabnzbd-history' });
            rows.forEach((r) => {
              ul.appendChild(el('li', {}, [
                el('strong', { textContent: r.name || '(unnamed)' }),
                el('br'),
                el('span', { className: 'srw-hint', textContent: `status: ${r.status || '?'}` }),
                el('br'),
                el('span', { className: 'srw-hint', textContent: `storage: ${r.storage || '(none)'}` }),
              ]));
            });
            resultsWrap.appendChild(ul);
          }
        } catch (err) {
          resultsWrap.textContent = `Error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    section.appendChild(resultsWrap);

    return section;
  }

  // Milestone 3 (2026-09-19): this project's own quality-profile
  // concept for indexer_search.py's release selection -- an ordered
  // list of allowed resolutions (best first) plus a cutoff (stored but
  // not yet used for selection, see indexer_search.py's module
  // docstring for why: confirmed against Whisparr's own source that
  // cutoff only governs upgrade decisions for an already-downloaded
  // file, never a fresh grab).
  async function buildQualityProfilesSection(settings, onChanged) {
    const { el, buildManagedList } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Quality Profiles' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Which resolutions indexer_search.py will grab for normal (non-VR) releases, best '
          + 'first -- see the VR note further down for how VR releases are handled instead.',
      }),
    ]);
    const listSection = el('div');
    const assignWrap = el('div');
    section.appendChild(listSection);
    section.appendChild(assignWrap);

    async function fetchProfiles() {
      try {
        const result = await window.SRW.runOp('quality_profiles_list');
        return Array.isArray(result) ? result : [];
      } catch (e) {
        return [];
      }
    }

    // The Assignment dropdown's own options depend on the same profile
    // list the managed list above shows -- refreshAll() re-fetches once
    // and rebuilds both, so adding/editing/deleting a profile (from
    // buildProfileRow/buildProfileForm below) keeps them in sync. Note
    // buildManagedList's own `refresh` (destructured as `refreshList`)
    // is intentionally NOT what buildProfileRow/buildProfileForm call --
    // they call refreshAll() instead, so the Assignment section updates
    // too.
    const { refresh: refreshList } = buildManagedList(listSection, {
      emptyMessage: 'No quality profiles configured yet.',
      fetchItems: fetchProfiles,
      buildEntry: (p) => buildProfileRow(p),
      buildAdder: () => buildProfileForm(null),
    });

    async function refreshAll() {
      await refreshList();
      const profiles = await fetchProfiles();
      assignWrap.innerHTML = '';

      assignWrap.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Assignment' }));
      buildAssignField('Normal', 'quality_profile_id', profiles);

      // VR is a plain on/off switch, NOT a profile picker (fixed
      // 2026-09-19, v0.40.2, after the user pointed out the original
      // "pick a profile for VR too" design was misleading) --
      // Whisparr's own "VR" quality is a dedicated tag, not a
      // resolution (confirmed live via its own qualitydefinition:
      // always reported as 1080p regardless of the real video
      // resolution, often 5K/8K for real VR content), so
      // indexer_search.py checks for that tag directly on a VR-flagged
      // release instead of matching any resolution list -- there's
      // nothing profile-shaped to configure here, just whether to
      // search for VR content at all.
      assignWrap.appendChild(el('p', {
        className: 'srw-hint',
        textContent: 'VR releases are matched by Whisparr\'s own dedicated "VR" quality tag, not by '
          + 'resolution (real VR content is often 5K/8K, which a resolution-based profile can\'t express '
          + 'anyway) -- so VR search is just on or off, no profile to pick.',
      }));
      const vrCheckbox = el('input', { type: 'checkbox' });
      vrCheckbox.checked = settings.vr_search_enabled === 'true';
      const vrStatus = el('span', { className: 'srw-key-status' });
      assignWrap.appendChild(el('label', {}, [vrCheckbox, ' Enable VR search']));
      assignWrap.appendChild(el('button', {
        className: 'srw-btn', textContent: 'Save',
        onClick: async () => {
          const value = vrCheckbox.checked ? 'true' : 'false';
          await window.SRW.runOp('settings_set', { key: 'vr_search_enabled', value });
          settings.vr_search_enabled = value;
          vrStatus.textContent = ' saved';
          setTimeout(() => { vrStatus.textContent = ''; }, 2000);
        },
      }));
      assignWrap.appendChild(vrStatus);
      assignWrap.appendChild(el('br'));

      // Size caps for non-VR releases (2026-09-28) -- VR has its own
      // limits (6K / 15 GB). Defaults from the library's own sizes.
      assignWrap.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Maximum file size (non-VR)' }));
      assignWrap.appendChild(el('p', {
        className: 'srw-hint',
        textContent: 'Larger releases are never grabbed or offered for review (e.g. whole site packs). '
          + 'Your library: 1080p is 99 % under 5.4 GB, 2160p 99 % under 11.4 GB. 0 = no limit.',
      }));
      [['max_file_gb', 'Up to 1440p (GB)', '8'], ['max_file_gb_2160', '2160p (GB)', '12']].forEach(([key, label, def]) => {
        const input = el('input', { type: 'number', min: '0', step: '0.5', value: settings[key] || def });
        const status = el('span', { className: 'srw-key-status' });
        assignWrap.appendChild(el('label', {}, [`${label}: `, input]));
        assignWrap.appendChild(el('button', {
          className: 'srw-btn', textContent: 'Save',
          onClick: async () => {
            const result = await window.SRW.runOp('settings_set', { key, value: input.value });
            status.textContent = result && result.error ? ` error: ${result.error}` : ' saved';
            setTimeout(() => { status.textContent = ''; }, 2000);
          },
        }));
        assignWrap.appendChild(status);
        assignWrap.appendChild(el('br'));
      });

      // Torrents (v0.231.0): the choice by seeders and the monitor of running torrents (torrent_health.py).
      assignWrap.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Torrents' }));
      assignWrap.appendChild(el('p', {
        className: 'srw-hint',
        textContent: 'Choice: the best resolution is taken when it has plenty of seeders; with only a few, a lower resolution that has several times as many seeders wins. '
          + 'A torrent nobody seeds, or whose size cannot fit the scene\'s length (probably mislabelled), is only a last resort. A name without a resolution is judged from the scene\'s length and the size. '
          + 'Monitor: the running torrents are checked regularly. One that makes no progress for the stall time, with nothing real downloaded, is replaced by another torrent when the search finds one '
          + '(the old one is removed with its data and never used again); without another torrent it is shown as stuck in Activity > Stuck. One with real progress is only flagged, never given up.',
      }));
      [['torrent_min_seeders', 'Fewer seeders than this = last resort', '1', { min: '0', step: '1' }],
        ['torrent_plenty_seeders', 'Plenty of seeders (the best resolution is taken at this many)', '10', { min: '1', step: '1' }],
        ['torrent_lower_res_factor', 'A lower resolution wins with this many times the seeders', '3', { min: '1', step: '0.5' }],
        ['torrent_health_minutes', 'Check running torrents every (minutes, 0 = off)', '30', { min: '0', step: '5' }],
        ['torrent_stall_hours', 'No progress for (hours) counts as stalled', '1', { min: '0.5', step: '0.5' }],
        ['torrent_real_progress_percent', 'Real progress (% downloaded): such a torrent is never given up', '5', { min: '0', max: '100', step: '1' }],
        ['torrent_alt_search_hours', 'Look for another torrent at most every (hours)', '6', { min: '0.5', step: '0.5' }]].forEach(([key, label, def, o]) => {
        const input = el('input', { type: 'number', min: o.min, max: o.max, step: o.step, value: settings[key] || def });
        const status = el('span', { className: 'srw-key-status' });
        assignWrap.appendChild(el('label', {}, [`${label}: `, input]));
        assignWrap.appendChild(el('button', {
          className: 'srw-btn', textContent: 'Save',
          onClick: async () => {
            const result = await window.SRW.runOp('settings_set', { key, value: input.value });
            status.textContent = result && result.error ? ` error: ${result.error}` : ' saved';
            setTimeout(() => { status.textContent = ''; }, 2000);
          },
        }));
        assignWrap.appendChild(status);
        assignWrap.appendChild(el('br'));
      });

      // Last resort for older scenes (v0.218.0): 2160p (scaled down to 1080p before the import) and SD, after everything in the profile.
      assignWrap.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Last resort for older scenes: 2160p and SD' }));
      assignWrap.appendChild(el('p', {
        className: 'srw-hint',
        textContent: 'When nothing in the profile exists for a scene that is at least this many days old, a 2160p release is taken and scaled down to 1080p HEVC before the import '
          + '(the original is not kept; the file has new hashes, so it is checked by its perceptual hash with a tolerance), and an SD release is taken as it is. 0 = off. '
          + 'The re-encode uses a quality target (variable bitrate): lower number = better and larger, 24 is a good start. Hardware encoding (Intel/AMD via /dev/dri) is used when it works, else the CPU. CPU speed: "veryfast" is several times quicker than "medium" for slightly larger files. H.264 gives larger files than H.265 but some chips can only encode that in hardware (try it if H.265 says "hardware not available"). "Where to encode" = encode worker hands the work to a faster machine running worker/srm_encode_worker.py (see INSTALL.md); while it is off, the download just waits. "Library on the worker" / "Downloads on the worker" tell the worker where it finds the sidecar\'s /media and /downloads folders on its own machine (sent with every job; the worker needs no --map then). Maintenance > Convert always uses a worker.',
      }));
      [['fallback_resolution_days', 'Days after the release date (0 = off)', 'number', '10', { min: '0', max: '365', step: '1' }],
        ['fallback_accept_sd', 'Accept SD as a last resort', 'select', 'true', { options: [['true', 'Yes'], ['false', 'No']] }],
        ['transcode_quality', 'Re-encode quality (16-32, lower = better)', 'number', '24', { min: '16', max: '32', step: '1' }],
        ['encode_target', 'Where to encode', 'select', 'nas', { options: [['nas', 'On this sidecar (the NAS)'], ['worker', 'On an encode worker (another machine)']] }],
        ['encode_worker_wait_hours', 'Encode here if no worker takes it within (hours, 0 = never)', 'number', '0', { min: '0', max: '720', step: '1' }],
        ['encode_worker_media_path', 'Library on the worker (folder as the worker machine sees it, e.g. /mnt/nas/library)', 'text', '', { placeholder: '/mnt/nas/library' }],
        ['encode_worker_downloads_path', 'Downloads on the worker (folder as the worker machine sees it)', 'text', '', { placeholder: '/mnt/nas/download/complete' }],
        ['transcode_codec', 'Re-encode format', 'select', 'h265', { options: [['h265', 'H.265 (HEVC, smaller files)'], ['h264', 'H.264 (larger files)']] }],
        ['transcode_preset', 'CPU speed', 'select', 'fast', { options: [['medium', 'Medium (best quality per size, slow)'], ['fast', 'Fast'], ['veryfast', 'Very fast (larger files)']] }],
        ['transcode_hw', 'Hardware encoding', 'select', 'auto', { options: [['auto', 'Automatic (when it works)'], ['off', 'Off (CPU only)']] }]].forEach(([key, label, kind, def, o]) => {
        const input = kind === 'select'
          ? el('select', {}, o.options.map(([v, text]) => el('option', { value: v, textContent: text })))
          : kind === 'text' ? el('input', { type: 'text', placeholder: o.placeholder || '', style: 'width: 22rem' })
            : el('input', { type: 'number', min: o.min, max: o.max, step: o.step });
        input.value = settings[key] || def;
        const status = el('span', { className: 'srw-key-status' });
        assignWrap.appendChild(el('label', {}, [`${label}: `, input]));
        assignWrap.appendChild(el('button', {
          className: 'srw-btn', textContent: 'Save',
          onClick: async () => {
            const result = await window.SRW.runOp('settings_set', { key, value: input.value });
            status.textContent = result && result.error ? ` error: ${result.error}` : ' saved';
            setTimeout(() => { status.textContent = ''; }, 2000);
          },
        }));
        assignWrap.appendChild(status);
        assignWrap.appendChild(el('br'));
      });
    }

    function buildAssignField(label, key, profiles) {
      const select = el('select', {}, [
        el('option', { value: '', textContent: '(not set)' }),
        ...profiles.map((p) => el('option', { value: String(p.id), textContent: `${p.name} (#${p.id})` })),
      ]);
      select.value = settings[key] || '';
      const status = el('span', { className: 'srw-key-status' });
      assignWrap.appendChild(el('label', {}, [`${label}: `, select]));
      assignWrap.appendChild(el('button', {
        className: 'srw-btn', textContent: 'Save',
        onClick: async () => {
          await window.SRW.runOp('settings_set', { key, value: select.value });
          settings[key] = select.value;
          status.textContent = ' saved';
          setTimeout(() => { status.textContent = ''; }, 2000);
        },
      }));
      assignWrap.appendChild(status);
      assignWrap.appendChild(el('br'));
    }

    function buildProfileRow(profile) {
      const row = el('div', { className: 'srw-dc-row' }, [
        el('strong', { textContent: `${profile.name} ` }),
        el('span', {
          className: 'srw-hint',
          textContent: `(${profile.resolutions.join(', ')}p${profile.cutoff_resolution ? `, cutoff ${profile.cutoff_resolution}p` : ''}) `,
        }),
        el('button', {
          className: 'srw-btn', textContent: 'Edit',
          onClick: () => row.replaceWith(buildProfileForm(profile)),
        }),
        el('button', {
          className: 'srw-btn srw-btn-danger', textContent: 'Delete',
          onClick: (e) => {
            window.SRW.confirmInline(e.currentTarget, `Delete quality profile "${profile.name}"?`, async () => {
              await window.SRW.runOp('quality_profiles_delete', { id: profile.id });
              await refreshAll();
              if (onChanged) onChanged();
            }, { danger: true });
          },
        }),
      ]);
      return row;
    }

    function buildProfileForm(existing) {
      const nameInput = el('input', { type: 'text', placeholder: 'Name', value: existing ? existing.name : '' });
      const resolutionsInput = el('input', {
        type: 'text', placeholder: 'Resolutions, best first (e.g. 2160,1080,720)',
        value: existing ? existing.resolutions.join(',') : '2160,1080,720',
      });
      const cutoffInput = el('input', {
        type: 'number', min: '1', step: '1', placeholder: 'Cutoff resolution (optional)',
        value: existing && existing.cutoff_resolution ? existing.cutoff_resolution : '',
      });
      const formStatus = el('span', { className: 'srw-key-status' });

      const form = el('div', { className: 'srw-dc-form' }, [
        el('label', {}, ['Name: ', nameInput]), el('br'),
        el('label', {}, ['Resolutions: ', resolutionsInput]), el('br'),
        el('label', {}, ['Cutoff: ', cutoffInput]), el('br'),
        el('button', {
          className: 'srw-btn srw-btn-primary', textContent: existing ? 'Save' : 'Add profile',
          onClick: async () => {
            const resolutions = resolutionsInput.value.split(',')
              .map((s) => parseInt(s.trim(), 10)).filter((n) => !Number.isNaN(n));
            if (!nameInput.value || resolutions.length === 0) {
              formStatus.textContent = ' name and at least one resolution are required';
              return;
            }
            const body = {
              name: nameInput.value, resolutions,
              cutoff_resolution: cutoffInput.value ? parseInt(cutoffInput.value, 10) : null,
            };
            try {
              if (existing) await window.SRW.runOp('quality_profiles_update', { id: existing.id, ...body });
              else await window.SRW.runOp('quality_profiles_create', body);
              await refreshAll();
              if (onChanged) onChanged();
            } catch (err) {
              formStatus.textContent = ` error: ${err.message}`;
            }
          },
        }),
        formStatus,
      ]);
      return form;
    }

    await refreshAll();
    return section;
  }

  // Only the scan interval is left here -- everything else that used to
  // live in this section belongs to one specific connection and moved
  // into that connection's own box above (2026-09-12). Named "Watcher
  // Schedule" rather than the bare "Schedule" it had right after that
  // split -- with everything else now grouped under a connection, a
  // lone "Schedule" box read as unexplained/orphaned; naming it after
  // what it actually schedules (this plugin's own scan loop) answers
  // that at a glance.
  // Search backend (2026-09-25) -- the explicit choice of where the
  // sidecar's own search gets candidates from, replacing the old
  // implicit "Prowlarr whenever configured" rule. See settings.py's
  // SEARCH_BACKENDS / resolve_search_backend() and search_sources.py.
  const SEARCH_BACKEND_LABELS = {
    whisparr: 'Whisparr (its own indexer search)',
    prowlarr: 'Prowlarr',
    indexers: 'Direct indexers (configured below)',
  };
  async function buildSearchBackendSection() {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Search backend' }),
    ]);
    const info = await window.SRW.runOp('settings_search_backend');
    if (info.error) {
      section.appendChild(el('p', { textContent: `Error: ${info.error}` }));
      return section;
    }
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Where this sidecar searches for releases (automatic search and hand-off). '
        + 'If the chosen backend isn\'t configured, searches are recorded as "search backend not available" '
        + '— there is no silent fallback to another backend.',
    }));
    const select = el('select');
    Object.entries(SEARCH_BACKEND_LABELS).forEach(([key, label]) => {
      const available = info.available[key];
      const opt = el('option', { value: key, textContent: available ? label : `${label} — not configured` });
      select.appendChild(opt);
    });
    select.value = info.backend;
    const status = el('span', { className: 'srw-key-status' });
    const saveBtn = el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'search_backend', value: select.value });
        status.textContent = result.error ? ` error: ${result.error}` : ' saved';
        if (!result.error && !info.available[select.value]) status.textContent += ' (warning: not configured yet)';
      },
    });
    section.appendChild(el('label', {}, ['Search with: ', select]));
    section.appendChild(saveBtn);
    section.appendChild(status);
    if (!info.explicit) {
      section.appendChild(el('p', {
        className: 'srw-hint',
        textContent: 'Not chosen explicitly yet — currently derived automatically (Prowlarr if configured, '
          + 'otherwise Whisparr). Saving makes the choice explicit.',
      }));
    }
    // Since 2026-09-25 never locked: ThePornDB/other providers always
    // use this backend (they bypass Whisparr under every preset but
    // full_whisparr). Only StashDB releases under the two Whisparr-search
    // presets ignore it -- say so instead of disabling the choice.
    if (['search_sidecar_import', 'full_whisparr'].includes(info.scope_preset)) {
      section.appendChild(el('p', {
        className: 'srw-hint',
        textContent: 'With the current Whisparr scope preset, Whisparr runs its own search for StashDB releases. '
          + 'This backend is still used for ThePornDB and other providers.',
      }));
    }
    return section;
  }

  // Direct Newznab indexers (2026-09-25, stage 2) -- used when the
  // search backend above is "Direct indexers". Same managed-list shape
  // as Download Clients. API keys are write-only: the list only says
  // whether one is set, and leaving the field empty on Edit keeps it.
  async function buildIndexersSection(onChanged) {
    const { el, buildManagedList } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Indexers (Newznab / Torznab)' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'Usenet (Newznab) and torrent (Torznab) indexers searched directly when the search backend is "Direct indexers". '
          + 'All enabled indexers are searched in parallel; lower priority number = preferred. A torrent result is only offered when an enabled torrent client exists.',
      }),
    ]);

    const { refresh } = buildManagedList(section, {
      emptyMessage: 'No indexers configured yet.',
      fetchItems: async () => {
        const result = await window.SRW.runOp('indexers_list');
        return Array.isArray(result) ? result : [];
      },
      buildEntry: (ix) => buildIndexerRow(ix),
      buildAdder: () => buildIndexerForm(null),
    });

    function buildIndexerRow(ix) {
      const status = el('span', { className: 'srw-key-status' });
      const row = el('div', { className: 'srw-dc-row' }, [
        el('strong', { textContent: `${ix.name} ` }),
        el('span', {
          className: 'srw-hint',
          textContent: `(${ix.type === 'torznab' ? 'Torznab, ' : ''}${ix.url}, categories ${ix.categories}, priority ${ix.priority}`
            + `${ix.type === 'torznab' ? `, min. seeders ${ix.minimum_seeders}` : ''}${ix.api_key_set ? '' : ', no API key'}${ix.enabled ? '' : ', disabled'}) `,
        }),
        el('button', {
          className: 'srw-btn', textContent: 'Check',
          onClick: async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            status.textContent = ' checking...';
            const result = await window.SRW.runOp('indexers_check', { id: ix.id });
            status.textContent = result.error ? ` error: ${result.error}` : ' ok';
            btn.disabled = false;
          },
        }),
        el('button', {
          className: 'srw-btn', textContent: ix.enabled ? 'Disable' : 'Enable',
          onClick: async () => {
            await window.SRW.runOp('indexers_update', { ...ix, api_key: '', enabled: !ix.enabled });
            await refresh();
            if (onChanged) onChanged();
          },
        }),
        el('button', {
          className: 'srw-btn', textContent: 'Edit',
          onClick: () => row.replaceWith(buildIndexerForm(ix)),
        }),
        el('button', {
          className: 'srw-btn srw-btn-danger', textContent: 'Delete',
          onClick: (e) => {
            window.SRW.confirmInline(e.currentTarget, `Delete indexer "${ix.name}"?`, async () => {
              await window.SRW.runOp('indexers_delete', { id: ix.id });
              await refresh();
              if (onChanged) onChanged();
            }, { danger: true });
          },
        }),
        status,
      ]);
      return row;
    }

    function buildIndexerForm(existing) {
      const nameInput = el('input', { type: 'text', placeholder: 'Name', value: existing ? existing.name : '' });
      const urlInput = el('input', { type: 'text', placeholder: 'https://indexer.example', value: existing ? existing.url : '' });
      const apiPathInput = el('input', { type: 'text', value: existing ? existing.api_path : '/api' });
      const apiKeyInput = el('input', {
        type: 'password', autocomplete: 'off',
        placeholder: existing && existing.api_key_set ? '(unchanged — enter to replace)' : 'API key',
      });
      const categoriesInput = el('input', { type: 'text', value: existing ? existing.categories : '6000' });
      const priorityInput = el('input', { type: 'number', min: '1', step: '1', value: existing ? existing.priority : '25' });
      const typeSelect = el('select', {}, [el('option', { value: 'newznab', textContent: 'Newznab (Usenet)' }), el('option', { value: 'torznab', textContent: 'Torznab (Torrent)' })]);
      typeSelect.value = existing ? existing.type : 'newznab';
      // *arr defaults: at least 1 seeder; seed ratio and seed time empty = the download client's own limits (seed time in minutes).
      const minSeedInput = el('input', { type: 'number', min: '0', step: '1', value: existing ? existing.minimum_seeders : '1' });
      const ratioInput = el('input', { type: 'number', min: '0', step: '0.1', placeholder: 'client default', value: existing && existing.seed_ratio != null ? existing.seed_ratio : '' });
      const timeInput = el('input', { type: 'number', min: '0', step: '1', placeholder: 'client default', value: existing && existing.seed_time != null ? existing.seed_time : '' });
      const torrentPart = el('div', {}, [
        el('label', {}, ['Minimum seeders: ', minSeedInput]), el('br'),
        el('label', {}, ['Seed ratio: ', ratioInput]), el('label', {}, [' Seed time (minutes): ', timeInput]), el('br'),
        el('p', { className: 'srw-hint', textContent: 'Seed ratio and seed time left empty: the torrent client\'s own limits apply (like the *arr applications).' }),
      ]);
      const syncType = () => { torrentPart.hidden = typeSelect.value !== 'torznab'; };
      typeSelect.addEventListener('change', syncType);
      syncType();
      const formStatus = el('span', { className: 'srw-key-status' });
      return el('div', { className: 'srw-dc-form' }, [
        el('label', {}, ['Type: ', typeSelect]), el('br'),
        el('label', {}, ['Name: ', nameInput]), el('br'),
        el('label', {}, ['URL: ', urlInput]), el('label', {}, [' API path: ', apiPathInput]), el('br'),
        el('label', {}, ['API key: ', apiKeyInput]), el('br'),
        el('label', {}, ['Categories: ', categoriesInput]), el('label', {}, [' Priority: ', priorityInput]), el('br'),
        torrentPart,
        el('button', {
          className: 'srw-btn srw-btn-primary', textContent: existing ? 'Save' : 'Add indexer',
          onClick: async () => {
            const body = {
              name: nameInput.value.trim(), url: urlInput.value.trim(), api_path: apiPathInput.value.trim() || '/api',
              api_key: apiKeyInput.value, categories: categoriesInput.value.trim() || '6000',
              priority: parseInt(priorityInput.value, 10) || 25, enabled: existing ? existing.enabled : true,
              type: typeSelect.value,
              minimum_seeders: minSeedInput.value === '' ? 1 : parseInt(minSeedInput.value, 10),
              seed_ratio: ratioInput.value === '' ? null : parseFloat(ratioInput.value),
              seed_time: timeInput.value === '' ? null : parseInt(timeInput.value, 10),
            };
            if (!body.name || !body.url) {
              formStatus.textContent = ' name and URL are required';
              return;
            }
            const result = existing
              ? await window.SRW.runOp('indexers_update', { id: existing.id, ...body })
              : await window.SRW.runOp('indexers_create', body);
            if (result.error) {
              formStatus.textContent = ` error: ${result.error}`;
              return;
            }
            await refresh();
            if (onChanged) onChanged();
          },
        }),
        formStatus,
      ]);
    }

    await refresh();
    return section;
  }

  // Releases page display options (2026-09-24; since 2026-09-26 the List
  // view's page size, see settings.py's list_view_page_size).
  // Watchlist source (2026-09-26, api/watchlist_source.py): the watchlist
  // follows either Stash's Favorite flag (original) or a dedicated
  // watchlist tag. The switch to the tag goes through the migration
  // (tags every current favorite first, then switches); the backend
  // refuses a direct switch to an empty tag.
  let watchlistMode = 'favorites';
  let watchlistTagName = 'Watchlist';
  let defaultWindowFrom = '';

  function buildWatchlistSourceSection(settings, rerender) {
    const { el, confirmInline } = window.SRW;
    const tagMode = watchlistMode === 'tag';
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Watchlist source' }),
      el('p', {
        className: 'srw-hint',
        textContent: tagMode
          ? `The watchlist follows the Stash tag "${watchlistTagName}" (child tags count too). Favorite is just a `
            + 'normal favorite. Add/remove the tag in Stash, or use the eye icon next to the heart on a '
            + 'Studio/Performer page.'
          : 'The watchlist follows Stash’s Favorite flag on Studios/Performers. Switch to a dedicated tag to curate '
            + 'the watchlist independently of your favorites (smaller watchlist = shorter scans).',
      }),
    ]);

    const tagInput = el('input', { type: 'text', value: watchlistTagName });
    const tagStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {}, ['Watchlist tag: ', tagInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'watchlist_tag_name', value: tagInput.value });
        if (result.error) { tagStatus.textContent = ` error: ${result.error}`; return; }
        watchlistTagName = tagInput.value.trim();
        tagStatus.textContent = ' saved';
      },
    }));
    section.appendChild(tagStatus);
    section.appendChild(el('br'));

    // Global default From (2026-09-26): entries without their own From
    // only store/load scenes released on or after this date.
    const defaultFromInput = el('input', { type: 'date', value: defaultWindowFrom });
    const defaultFromStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {
      title: 'Entries without their own From date only store/load scenes released on or after this date '
        + '(scan, Missing from Catalog, Auto → Wishlist). Empty = no limit.',
    }, ['Default date window From: ', defaultFromInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'default_window_from', value: defaultFromInput.value });
        if (result.error) { defaultFromStatus.textContent = ` error: ${result.error}`; return; }
        defaultWindowFrom = defaultFromInput.value;
        defaultFromStatus.textContent = ' saved';
      },
    }));
    section.appendChild(defaultFromStatus);
    section.appendChild(el('br'));

    // Catalog of new watchlist entries (v0.198.1)
    const catalogSelect = el('select', {}, [
      el('option', { value: 'off', textContent: 'Off (load by hand per year)' }),
      el('option', { value: 'ask', textContent: 'Ask after Watch/Favorite on the Search page' }),
      el('option', { value: 'auto', textContent: 'Automatically in the background' }),
    ]);
    catalogSelect.value = settings.catalog_on_watch || 'off';
    const catalogStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {
      title: 'What happens when a Studio/Performer newly lands on the watchlist. Automatic loads every year inside the date window (From above) '
        + 'as "Missing from Catalog" entries. StashDB and added stash-boxes (FansDB, JAVStash ...); not ThePornDB.',
    }, ['Load the catalog of new entries: ', catalogSelect]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'catalog_on_watch', value: catalogSelect.value });
        catalogStatus.textContent = result.error ? ` error: ${result.error}` : ' saved';
      },
    }));
    section.appendChild(catalogStatus);
    section.appendChild(el('br'));

    const status = el('p', { className: 'srw-key-status' });
    if (tagMode) {
      section.appendChild(el('button', {
        className: 'srw-btn', textContent: 'Switch back to Favorites',
        onClick: (e) => confirmInline(e.currentTarget,
          'Watchlist follows Favorites again: entities that only have the tag (not starred) drop off the watchlist '
          + 'at the next scan. The tag itself stays in Stash. Switch?', async () => {
            const result = await window.SRW.runOp('settings_set', { key: 'watchlist_source', value: 'favorites' });
            if (result.error) { status.textContent = `Error: ${result.error}`; return; }
            rerender();
          }),
      }));
    } else {
      const checkBtn = el('button', {
        className: 'srw-btn', textContent: 'Check',
        title: 'Counts what the switch would do -- changes nothing',
        onClick: async () => {
          status.textContent = 'Checking...';
          const plan = await window.SRW.runOp('watchlist_migrate', { dry_run: true });
          if (plan.error) { status.textContent = `Error: ${plan.error}`; return; }
          const total = plan.studio.to_tag + plan.performer.to_tag;
          status.textContent = `${plan.studio.favorites} favorite studio(s) and ${plan.performer.favorites} favorite `
            + `performer(s); ${total} still need the tag "${plan.tag_name}"`
            + `${plan.tag_exists ? '' : ' (tag will be created)'}.`;
        },
      });
      const switchBtn = el('button', {
        className: 'srw-btn srw-btn-primary', textContent: 'Switch to tag…',
        onClick: (e) => confirmInline(e.currentTarget,
          `Adds the tag "${watchlistTagName}" to every current favorite Studio/Performer, checks that all of them `
          + 'have it, and only then switches -- the watchlist itself stays the same, no re-scan. Takes about '
          + '0.75 s per entity in Stash. Every change also fires other plugins’ Performer/Studio update hooks. '
          + 'Start?', startMigration),
      });
      section.appendChild(checkBtn);
      section.appendChild(switchBtn);
    }
    section.appendChild(status);

    let pollTimer = null;
    function showMigration(m) {
      if (!m || m.state === 'idle') return;
      if (m.state === 'running') {
        status.textContent = m.phase === 'tagging'
          ? `Tagging… ${m.done} / ${m.total}`
          : (m.phase === 'verifying' ? 'Checking every favorite has the tag…' : 'Preparing…');
      } else if (m.state === 'done') {
        status.textContent = 'Done -- the watchlist now follows the tag.';
      } else if (m.state === 'failed') {
        status.textContent = `Stopped: ${m.error}`;
      }
    }
    async function poll() {
      const m = await window.SRW.runOp('watchlist_migrate_status');
      showMigration(m);
      if (m && m.state === 'running') {
        pollTimer = setTimeout(poll, 3000);
      } else if (m && m.state === 'done') {
        watchlistMode = 'tag';
        window.SRW.notify('Watchlist', `The watchlist now follows the tag "${watchlistTagName}".`, 'success');
        setTimeout(rerender, 1500);
      }
    }
    async function startMigration() {
      const result = await window.SRW.runOp('watchlist_migrate', {});
      if (result.error) { status.textContent = `Error: ${result.error}`; return; }
      showMigration(result);
      clearTimeout(pollTimer);
      pollTimer = setTimeout(poll, 2000);
    }
    // A migration still running from an earlier visit/tab: pick it up.
    if (!tagMode) {
      window.SRW.runOp('watchlist_migrate_status').then((m) => {
        showMigration(m);
        if (m && m.state === 'running') pollTimer = setTimeout(poll, 3000);
      }).catch(() => {});
    }
    return section;
  }

  // "Probably inactive" review list (2026-09-26, api/inactive_check.py):
  // watchlist entities whose newest scene is older than N months (or
  // that have no scenes / were deleted on StashDB). Nothing is removed
  // automatically -- Keep (not proposed again unless a new reason shows
  // up) or Remove from watchlist (removes the tag / Favorite in Stash).
  function buildInactiveReviewSection(settings) {
    const { el, confirmInline } = window.SRW;
    const summary = el('summary', {}, [el('span', { textContent: 'Probably inactive (…)' })]);
    const body = el('div');
    const section = el('details', { className: 'srw-settings-section srw-inactive-review' }, [summary, body]);
    window.SRW.rememberOpen(section, 'settings:inactive-review', false);

    const runStatus = el('span', { className: 'srw-key-status' });
    const list = el('div');
    body.appendChild(el('p', {
      className: 'srw-hint',
      textContent: `Watchlist entries whose newest scene (by release date) is older than ${settings.inactive_after_months || '18'} months, that have `
        + 'no scenes, or that were deleted/merged on StashDB. Career end and death dates are only shown as '
        + 'extra information. Nothing is removed automatically. The threshold and the refresh interval are set in Settings > Jobs.',
    }));
    const runBtn = el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Check now',
      onClick: async () => {
        runBtn.disabled = true;
        const result = await window.SRW.runOp('watchlist_inactive_run');
        if (result.error) { runStatus.textContent = ` error: ${result.error}`; runBtn.disabled = false; return; }
        poll();
      },
    });
    body.appendChild(runBtn);
    body.appendChild(runStatus);

    // Bulk keep / remove (v0.131.1): a checkbox per row. "Select all" selects the entries of the CURRENT PAGE only (v0.157.0); when more
    // entries match the filter, a button next to it selects all of them on purpose ("Select all N matching").
    const selected = new Set();
    const itemKey = (item) => `${item.entity_type}:${item.local_id}`;
    let lastFilter = null;
    let baseFetch = null;
    let pageItems = []; // selectable entries of the page shown
    let matchingItemsNow = []; // selectable entries matching the current filter, all pages
    const selectAll = el('input', { type: 'checkbox', title: 'Select every entry on this page' });
    const selectMore = el('button', { type: 'button', className: 'srw-btn' });
    selectMore.hidden = true;
    const selectLabel = el('span', { className: 'srw-hint' });
    const bulkKeep = el('button', { className: 'srw-btn', textContent: 'Keep selected', title: 'Keep the selected entries on the watchlist' });
    const bulkRemove = el('button', { className: 'srw-btn srw-btn-danger', textContent: 'Remove selected' });
    const bulkStatus = el('span', { className: 'srw-hint' });
    const bulkBar = el('div', { className: 'srw-inactive-bulk' }, [
      el('label', {}, [selectAll, ' Select all on this page']), selectMore, selectLabel, bulkKeep, bulkRemove, bulkStatus]);
    body.appendChild(bulkBar);
    body.appendChild(list);

    // The "select all" box follows the page: checked when every entry shown is selected; the "all matching" button only when there is more.
    function syncSelectAll() {
      selectAll.checked = pageItems.length > 0 && pageItems.every((x) => selected.has(itemKey(x)));
      const rest = matchingItemsNow.filter((x) => !selected.has(itemKey(x))).length;
      selectMore.hidden = !(selectAll.checked && rest > 0);
      selectMore.textContent = `Select all ${matchingItemsNow.length} matching`;
    }

    function updateBulk() {
      const n = selected.size;
      selectLabel.textContent = ` ${n} selected`;
      bulkKeep.disabled = bulkRemove.disabled = n === 0;
      bulkRemove.textContent = `${removeLabel} (selected)`;
      syncSelectAll();
    }

    selectAll.addEventListener('change', () => {
      pageItems.forEach((x) => { if (selectAll.checked) selected.add(itemKey(x)); else selected.delete(itemKey(x)); });
      updateBulk();
      view.reload();
    });
    selectMore.addEventListener('click', () => {
      matchingItemsNow.forEach((x) => selected.add(itemKey(x)));
      updateBulk();
      view.reload();
    });

    async function runBulk(kind) {
      const todo = currentItems.filter((x) => selected.has(itemKey(x)));
      bulkKeep.disabled = bulkRemove.disabled = true;
      let done = 0;
      const failed = [];
      for (const item of todo) {
        bulkStatus.textContent = ` ${kind === 'keep' ? 'keeping' : 'removing'} ${done + 1} / ${todo.length}…`;
        const result = kind === 'keep'
          ? await window.SRW.runOp('watchlist_inactive_keep', { entity_type: item.entity_type, local_id: item.local_id, reasons: item.reasons.map((r) => r.key) })
          : await window.SRW.runOp('watchlist_set_watch_state', { entity_type: item.entity_type, local_id: item.local_id, watched: false });
        if (result && result.error) { failed.push(`${item.name}: ${result.error}`); continue; }
        done += 1;
        selected.delete(itemKey(item));
        currentItems = currentItems.filter((x) => x !== item);
      }
      bulkStatus.textContent = '';
      selectAll.checked = false;
      summary.querySelector('span').textContent = `Probably inactive (${currentItems.length})`;
      updateBulk();
      view.reload();
      if (failed.length) window.SRW.notify('Probably inactive', `${done} done, ${failed.length} failed: ${failed.slice(0, 3).join('; ')}${failed.length > 3 ? ' …' : ''}`, 'error');
      else window.SRW.notify('Probably inactive', kind === 'keep' ? `${done} kept on the watchlist.` : `${done} removed from the watchlist -- no longer scanned from the next scan on.`, 'success');
    }
    bulkKeep.addEventListener('click', () => runBulk('keep'));
    bulkRemove.addEventListener('click', (e) => confirmInline(e.currentTarget,
      `Remove ${selected.size} selected entr${selected.size === 1 ? 'y' : 'ies'} from the watchlist?`, () => runBulk('remove'), { danger: true }));

    let pollTimer = null;
    async function poll() {
      const data = await window.SRW.runOp('watchlist_inactive_list');
      render(data);
      if (data.job && data.job.running) {
        clearTimeout(pollTimer);
        pollTimer = setTimeout(poll, 3000);
      }
    }

    function render(data) {
      if (!data || data.error) { runStatus.textContent = ` error: ${data && data.error}`; return; }
      const running = data.job && data.job.running;
      runBtn.disabled = !!running;
      const last = data.last_run;
      runStatus.textContent = running
        ? ` checking… ${data.job.phase || ''}`
        : (last ? ` last checked ${last.checked_at.replace('T', ' ').slice(0, 16)} UTC`
            + `${last.errors && last.errors.length ? ` (${last.errors.length} error(s))` : ''}` : ' not checked yet');
      summary.querySelector('span').textContent = `Probably inactive (${data.items.length})`;
      currentItems = data.items;
      removeLabel = data.mode === 'tag' ? 'Remove from watchlist' : 'Unfavorite';
      updateBulk();
      if (!view) {
        baseFetch = window.SRW.localPageFetcher(() => currentItems, {
          text: (x) => x.name, type: (x) => x.entity_type, date: (x) => x.newest_scene_date,
        });
        // Filter + paging (2026-09-26): name, studio/performer, newest-scene date range.
        view = window.SRW.buildFilteredPagedList({
          filters: ['search', 'type', 'dates'],
          perPage: 40,
          emptyText: 'Nothing to review.',
          fetchPage: async (f, page, perPage) => {
            lastFilter = f;
            const shown = await baseFetch(f, page, perPage);
            pageItems = (shown.items || []).filter((x) => x.local_id);
            matchingItemsNow = ((await baseFetch(f, 1, 1000000)).items || []).filter((x) => x.local_id); // local data, no request
            syncSelectAll();
            return shown;
          },
          renderItems: renderInactiveItems,
        });
        list.appendChild(view.node);
      }
      view.reload();
    }

    let view = null;
    let currentItems = [];
    let removeLabel = 'Remove from watchlist';

    function renderInactiveItems(items) {
      const ul = el('ul', { className: 'srw-inactive-list' });
      items.forEach((item) => {
        const li = el('li');
        const name = item.local_id
          ? el('a', { href: `/${item.entity_type}s/${item.local_id}`, textContent: item.name })
          : el('span', { textContent: item.name });
        const reasons = item.reasons.map((r) => r.text).join(' · ');
        const keepBtn = el('button', {
          className: 'srw-btn', textContent: 'Keep',
          title: 'Keep on the watchlist -- only proposed again if a new reason shows up',
          onClick: async () => {
            keepBtn.disabled = true;
            const result = await window.SRW.runOp('watchlist_inactive_keep', {
              entity_type: item.entity_type, local_id: item.local_id, reasons: item.reasons.map((r) => r.key),
            });
            if (result.error) { keepBtn.disabled = false; window.SRW.notify('Keep', result.error, 'error'); return; }
            currentItems = currentItems.filter((x) => x !== item);
            selected.delete(itemKey(item));
            updateBulk();
            summary.querySelector('span').textContent = `Probably inactive (${currentItems.length})`;
            view.reload();
          },
        });
        const removeBtn = el('button', {
          className: 'srw-btn srw-btn-danger', textContent: removeLabel,
          onClick: (e) => confirmInline(e.currentTarget, `Remove "${item.name}" from the watchlist?`, async () => {
            const result = await window.SRW.runOp('watchlist_set_watch_state', {
              entity_type: item.entity_type, local_id: item.local_id, watched: false,
            });
            if (result.error) { window.SRW.notify('Watchlist', result.error, 'error'); return; }
            currentItems = currentItems.filter((x) => x !== item);
            selected.delete(itemKey(item));
            updateBulk();
            summary.querySelector('span').textContent = `Probably inactive (${currentItems.length})`;
            view.reload();
            const flagged = (result.decision && result.decision.flagged) || [];
            window.SRW.notify('Removed from watchlist', `"${item.name}" is no longer scanned from the next scan on.`
              + (flagged.length ? ' It still has open releases -- decide on the Releases page.' : ''), 'success');
          }, { danger: true }),
        });
        if (!item.local_id) { keepBtn.disabled = true; removeBtn.disabled = true; }
        const pick = el('input', { type: 'checkbox' });
        if (!item.local_id) pick.disabled = true;
        pick.checked = selected.has(itemKey(item));
        pick.addEventListener('change', () => { if (pick.checked) selected.add(itemKey(item)); else selected.delete(itemKey(item)); updateBulk(); });
        li.appendChild(pick);
        li.appendChild(window.SRW.entityIcon(item.entity_type));
        li.appendChild(name);
        li.appendChild(el('span', { className: 'srw-inactive-reasons', textContent: ` — ${reasons}` }));
        li.appendChild(keepBtn);
        li.appendChild(removeBtn);
        ul.appendChild(li);
      });
      return ul;
    }

    async function load() {
      try { await poll(); } catch (e) { runStatus.textContent = ` error: ${e.message}`; }
    }
    load();
    return section;
  }

  // Hidden studios (2026-10-03): sub-studios hidden from the Calendar/List (e.g. "Giorgio Grandi" under the watched "Anal Vids").
  function buildHiddenStudiosSection() {
    const { el } = window.SRW;
    const list = el('div');
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Hidden studios' }),
      el('p', {
        className: 'srw-hint',
        textContent: 'A studio you hid with “Hide studio” on a release. Its releases that came in through a watched (parent) studio stay out of the '
          + 'Calendar and List -- new ones too; releases found through a favourite performer are not affected. “Show again” restores them.',
      }),
      list,
    ]);
    async function load() {
      const r = await window.SRW.runOp('muted_studios_list');
      if (r.error) { list.textContent = `error: ${r.error}`; return; }
      if (!(r.studios || []).length) { list.replaceChildren(el('div', { className: 'srw-hint', textContent: 'No hidden studios.' })); return; }
      list.replaceChildren(...r.studios.map((s) => {
        const btn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Show again' });
        btn.addEventListener('click', async () => {
          btn.disabled = true;
          const res = await window.SRW.runOp('muted_studios_unmute', { studio_id: s.studio_id });
          if (res.error) { window.SRW.notify('Hidden studios', res.error, 'error'); btn.disabled = false; return; }
          window.SRW.notify('Hidden studios', `“${s.name}” is shown again (${res.restored} release(s) restored).`, 'success');
          load();
        });
        return el('div', { className: 'srw-cmp-id' }, [
          el('div', {}, [el('strong', { textContent: s.name || s.studio_id }), el('span', { className: 'srw-hint', textContent: ` ${s.hidden} release(s) hidden · ${s.provider}` })]),
          btn]);
      }));
    }
    load();
    return section;
  }

  function buildReleasesPageSection(settings) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Releases page' }),
    ]);
    const limitInput = el('input', {
      type: 'number', min: '20', max: '1000', step: '20', value: settings.list_view_page_size || '100',
    });
    const status = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {}, ['List view: entries per page: ', limitInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'list_view_page_size', value: limitInput.value });
        status.textContent = result.error ? ` error: ${result.error}` : ' saved';
      },
    }));
    section.appendChild(status);
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Filters and "Dismiss all N matching" always cover every matching release, on all pages; "Dismiss N on this page" only the current page. Applies to the scene and the movie lists.',
    }));
    return section;
  }

  // "Restart sidecar" (v0.213.3): the container ends its process and Docker starts it again (restart: unless-stopped), so no CLI is needed after an
  // import or an update of the settings that only a restart picks up. Waits for the sidecar to answer again and says so.
  function buildRestartControl() {
    const { el, confirmInline, notify } = window.SRW;
    const status = el('span', { className: 'srw-key-status' });
    const btn = el('button', { className: 'srw-btn', textContent: 'Restart sidecar',
      title: 'Restarts the sidecar container (Docker starts it again); running jobs are interrupted and begin again afterwards' });
    btn.addEventListener('click', () => confirmInline(btn,
      'Restart the sidecar now? A download that is being imported at this moment is interrupted and picked up again afterwards; the pages reconnect by themselves.', async () => {
        status.textContent = ' restarting…';
        const r = await window.SRW.runOp('system_restart');
        if (r && r.error) { status.textContent = ` error: ${r.error}`; return; }
        const began = Date.now();
        await new Promise((res) => setTimeout(res, 4000)); // it first has to go down
        for (let i = 0; i < 40; i++) {
          let back = false;
          try { const c = await window.SRW.runOp('connections_check', { only: 'stash' }); back = !!(c && !c.error); } catch (e) { back = false; }
          if (back) {
            const sec = Math.round((Date.now() - began) / 1000);
            status.textContent = ` back after ${sec} s`;
            notify('Sidecar', `Restarted, answering again after ${sec} s.`, 'success');
            return;
          }
          await new Promise((res) => setTimeout(res, 3000));
        }
        status.textContent = ' did not answer within two minutes -- check the container (docker compose logs)';
      }));
    return el('span', {}, [btn, status]);
  }

  // Backup & move (v0.173.0): export the user's own data to a ZIP and import it into a FRESH installation (a move, a new install, a backup that can
  // be read). No URLs, keys, passwords or paths go into the file; a new installation gets those entered like a new user. The import counts first
  // (dry run) and only then writes, after a backup of the database; see api/data_transfer.py.
  function buildTransferSection() {
    const { el, confirmInline, notify } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Backup & move' }),
      el('p', { className: 'srw-hint', textContent: 'Export packs your own data into one ZIP file (data/exports/ on the machine the sidecar runs on) so it can be imported into a fresh installation. '
        + 'URLs, API keys, passwords and local paths are never exported; a new installation gets them entered like a new user. Running downloads and pending search results are not carried.' }),
    ]);
    section.appendChild(el('div', {}, [buildRestartControl(), el('span', { className: 'srw-hint', textContent: ' after an import the sidecar has to restart once' })]));
    const groupBox = el('div');
    const exportStatus = el('span', { className: 'srw-key-status' });
    const fileBox = el('div');
    const checks = {};
    const humanSize = (b) => (b > 1048576 ? `${(b / 1048576).toFixed(1)} MB` : `${Math.max(1, Math.round(b / 1024))} KB`);
    const rowsText = (tables) => Object.entries(tables || {}).filter(([, n]) => n).map(([t, n]) => `${t} ${n}`).join(', ');

    const exportBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Export' });
    exportBtn.addEventListener('click', async () => {
      const groups = Object.entries(checks).filter(([, box]) => box.checked).map(([k]) => k);
      if (!groups.length) { exportStatus.textContent = ' tick at least one group'; return; }
      exportBtn.disabled = true; exportStatus.textContent = ' exporting…';
      const r = await window.SRW.runOp('transfer_export', { groups });
      exportBtn.disabled = false;
      if (r.error) { exportStatus.textContent = ` error: ${r.error}`; return; }
      exportStatus.textContent = ` ${r.name} (${humanSize(r.size)}) written; ${r.skipped_settings} setting(s) with a URL / key / path were left out`;
      await load();
    });

    async function importFlow(file, row) {
      const out = el('div', { className: 'srw-hint' });
      row.appendChild(out);
      out.textContent = 'checking…';
      const plan = await window.SRW.runOp('transfer_import_check', { name: file.name });
      if (plan.error) { out.textContent = `error: ${plan.error}`; return; }
      out.replaceChildren(
        el('div', { textContent: `This would import (file from ${plan.created_at}, version ${plan.version}): ${rowsText(plan.tables) || 'nothing'}.` }),
        ...plan.warnings.map((w) => el('div', { textContent: `Note: ${w}` })),
        ...plan.blockers.map((b) => el('div', { className: 'srw-error', textContent: `Cannot import: ${b}` })),
      );
      if (plan.blockers.length || !Object.keys(plan.tables).length) return;
      const go = el('button', { className: 'srw-btn srw-btn-danger', textContent: 'Import now' });
      go.addEventListener('click', () => confirmInline(go,
        'Import this file into this installation? A backup of the database is made first; nothing is changed if any row fails.', async () => {
          out.textContent = 'importing…';
          const r = await window.SRW.runOp('transfer_import_apply', { name: file.name });
          if (r.error) { out.textContent = `error: ${r.error}`; return; }
          out.replaceChildren(el('div', { textContent: `Imported: ${rowsText(r.rows)}. Backup of the database before the import: data/backups/${r.backup}.` }),
            el('div', { textContent: r.note }));
          out.appendChild(buildRestartControl());
          notify('Import', 'Done. Restart the sidecar once (button below the result).', 'success');
        }, { danger: true }));
      out.appendChild(go);
    }

    async function load() {
      const r = await window.SRW.runOp('transfer_groups');
      if (r.error) { groupBox.textContent = `error: ${r.error}`; return; }
      if (!Object.keys(checks).length) {
        groupBox.replaceChildren(...r.groups.map((g) => {
          const box = el('input', { type: 'checkbox', checked: g.default });
          checks[g.key] = box;
          const n = Object.values(g.tables).reduce((a, b) => a + b, 0);
          return el('div', {}, [el('label', {}, [box, ` ${g.label} `, el('span', { className: 'srw-hint', textContent: `(${n} row${n === 1 ? '' : 's'})` })])]);
        }));
      }
      const sidecarUrl = ((await window.SRW.getSettings()) || {}).sidecarUrl || '';
      fileBox.replaceChildren(r.files.length ? el('ul', { className: 'srw-release-list' }, r.files.map((f) => {
        const row = el('li', { className: 'srw-release-item' });
        row.append(
          el('span', { className: 'srw-release-title', textContent: f.name }),
          el('span', { className: 'srw-release-date', textContent: `${f.modified} · ${humanSize(f.size)}${f.error ? '' : ` · ${f.rows} rows · version ${f.version}`}` }),
          f.error ? el('span', { className: 'srw-hint', textContent: f.error }) : null,
          el('a', { className: 'srw-btn srw-btn-sm', href: `${sidecarUrl}/transfer/download?name=${encodeURIComponent(f.name)}`, textContent: 'Download',
            title: 'The file itself (the browser must reach the sidecar); it also lies in data/ on the sidecar machine' }),
          f.error ? null : el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Check import (dry run)', onClick: (e) => { e.currentTarget.remove(); importFlow(f, row); } }),
        );
        return row;
      })) : el('p', { className: 'srw-hint', textContent: 'No export or import file yet. To import on another machine drop the ZIP on the area above (or put it into the folder data/imports/ of that installation).' }));
    }
    // Upload of an export file (v0.180.0): drop it on the area or choose it; it goes to the sidecar over a WebSocket (Stash's content security policy
    // does not let the page POST to another host), in 1 MB pieces, and lands in data/imports/ -- no copying of files on the machine needed.
    const uploadStatus = el('span', { className: 'srw-key-status' });
    const fileInput = el('input', { type: 'file', accept: '.zip,application/zip', hidden: true });
    const dropZone = el('div', { className: 'srw-dropzone' }, [
      'Import: drop an export file (.zip) here, or ',
      el('button', { type: 'button', className: 'srw-btn', textContent: 'Choose file…', onClick: () => fileInput.click() }),
      fileInput, uploadStatus,
    ]);
    async function uploadExportFile(file) {
      if (!file) return;
      if (!/\.zip$/i.test(file.name)) { uploadStatus.textContent = ' only .zip files (exports of this program) can be uploaded'; return; }
      const sidecarUrl = (((await window.SRW.getSettings()) || {}).sidecarUrl || '').replace(/\/$/, '');
      uploadStatus.textContent = ' connecting…';
      dropZone.classList.add('srw-dropzone-busy');
      try {
        const result = await new Promise((resolve, reject) => {
          const ws = new WebSocket(window.SRW.sidecarWsUrl(sidecarUrl, '/transfer/upload'));
          ws.binaryType = 'arraybuffer';
          const CHUNK = 1024 * 1024;
          let offset = 0; let started = false;
          const sendNext = async () => {
            if (offset >= file.size) { ws.send('end'); return; }
            const piece = await file.slice(offset, offset + CHUNK).arrayBuffer();
            offset += piece.byteLength;
            ws.send(piece);
          };
          ws.onopen = () => ws.send(JSON.stringify({ name: file.name, size: file.size }));
          ws.onerror = () => reject(new Error('the sidecar could not be reached (Sidecar API URL in the plugin settings; the browser must reach it)'));
          ws.onclose = () => { if (!started || offset < file.size) reject(new Error('the connection closed before the upload was complete')); };
          ws.onmessage = async (ev) => {
            const msg = JSON.parse(ev.data);
            if (msg.error) { reject(new Error(msg.error)); ws.close(); return; }
            if (msg.ready) { started = true; await sendNext(); return; }
            if (msg.got !== undefined) {
              uploadStatus.textContent = ` uploading ${Math.round((msg.got / file.size) * 100)} %`;
              await sendNext();
              return;
            }
            if (msg.ok) { resolve(msg); ws.close(); }
          };
        });
        uploadStatus.textContent = ` ${result.name} arrived; check it below`;
        notify('Import', `${file.name} was uploaded.`, 'success');
        await load();
      } catch (e) {
        uploadStatus.textContent = ` upload failed: ${e.message}`;
      } finally {
        dropZone.classList.remove('srw-dropzone-busy');
        fileInput.value = '';
      }
    }
    fileInput.addEventListener('change', () => uploadExportFile(fileInput.files[0]));
    ['dragenter', 'dragover'].forEach((t) => dropZone.addEventListener(t, (e) => { e.preventDefault(); dropZone.classList.add('srw-dropzone-over'); }));
    ['dragleave', 'drop'].forEach((t) => dropZone.addEventListener(t, (e) => { e.preventDefault(); dropZone.classList.remove('srw-dropzone-over'); }));
    dropZone.addEventListener('drop', (e) => uploadExportFile(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]));

    section.append(groupBox, el('div', { className: 'srw-toolbar' }, [exportBtn, exportStatus]), el('h4', { textContent: 'Files (data/exports and data/imports)' }), dropZone, fileBox);
    load();
    return section;
  }

  // One labeled setting row in the usual shape (label + input/select + Save + status), wired to a single settings_set key (v0.146.0).
  // Which result categories the searches may use (v0.247.0, category_filter.py): a ticked box = searched, unticked = dropped from every result list.
  // The ids are NZBFinder's sub categories of XXX (6000).
  const SEARCH_CATEGORIES = [[6010, 'DVD'], [6030, 'SD'], [6040, 'HD'], [6045, 'UHD'], [6046, 'VR'], [6047, 'Creator'], [6048, 'JAV'],
    [6050, 'Pack'], [6060, 'Imageset'], [6999, 'Other']];
  function appendCategoryRows(section, settings) {
    const { el } = window.SRW;
    const off = new Set(String(settings.excluded_categories != null ? settings.excluded_categories : '6050,6060')
      .split(',').map((s) => parseInt(s, 10)).filter(Boolean));
    const status = el('span', { className: 'srw-key-status' });
    const boxes = SEARCH_CATEGORIES.map(([id, name]) => {
      const box = el('input', { type: 'checkbox' });
      box.checked = !off.has(id);
      box.dataset.id = String(id);
      box.addEventListener('change', async () => {
        const excluded = boxes.filter((b) => !b.checked).map((b) => b.dataset.id).join(',');
        const result = await window.SRW.runOp('settings_set', { key: 'excluded_categories', value: excluded });
        status.textContent = result && result.error ? ` error: ${result.error}` : ' saved';
      });
      return box;
    });
    section.appendChild(el('p', { className: 'srw-hint', textContent: 'Result categories to search (applies to every search, the RSS pull and manual search): an unticked category is dropped from the results. '
      + 'A pack of many scenes or a set of pictures is never the one scene wanted, so Pack and Imageset are off by default; JAV and Creator stay on for the stash-boxes that have them.' }));
    section.appendChild(el('div', { className: 'srw-category-row' }, SEARCH_CATEGORIES.map(([id, name], i) =>
      el('label', { style: 'margin-right:14px;white-space:nowrap' }, [boxes[i], ` ${name} (${id})`]))));
    section.appendChild(status);
  }

  function appendSettingRow(section, { label, key, value, hint, min, max, step, options }) {
    const { el } = window.SRW;
    const control = options
      ? el('select', {}, options.map(([v, text]) => el('option', { value: v, textContent: text })))
      : el('input', { type: 'number', min, max, step });
    control.value = value;
    const status = el('span', { className: 'srw-key-status' });
    if (hint) section.appendChild(el('p', { className: 'srw-hint', textContent: hint }));
    section.appendChild(el('label', {}, [`${label}: `, control]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key, value: control.value });
        status.textContent = result && result.error ? ` error: ${result.error}` : ' saved';
      },
    }));
    section.appendChild(status);
  }

  // "Upgrade search" (v0.155.0): the automatic search for the candidates of Maintenance > Upgrade wanted -- a job, so its three settings live
  // with the other jobs (they used to sit in the Upgrade wanted area). It rides on the indexer-search loop.
  function buildUpgradeSearchSection(settings) {
    const section = window.SRW.el('div', { className: 'srw-settings-section' }, [
      window.SRW.el('h4', { textContent: 'Upgrade search' }),
      window.SRW.el('p', { className: 'srw-hint', textContent: 'Searches a few of the candidates of Maintenance > Upgrade wanted by itself and grabs only a really better file. '
        + 'Every candidate searched is a request to your search backend, so keep “per run” small.' }),
    ]);
    appendSettingRow(section, {
      label: 'Automatic upgrade search', key: 'upgrade_search_enabled', value: settings.upgrade_search_enabled === 'true' ? 'true' : 'false',
      options: [['false', 'Off'], ['true', 'On']],
    });
    appendSettingRow(section, {
      label: 'Dry-run button on Upgrade wanted', key: 'upgrade_dry_run_button', value: settings.upgrade_dry_run_button === 'true' ? 'true' : 'false',
      options: [['false', 'Hidden'], ['true', 'Shown']],
    });
    appendSettingRow(section, { label: 'VR file below this bitrate is an upgrade candidate (Mbit/s)', key: 'vr_upgrade_min_mbps', value: settings.vr_upgrade_min_mbps || '8', min: '1', max: '200', step: '1' });
    appendSettingRow(section, { label: 'At most every (hours, 1-168)', key: 'upgrade_search_interval_hours', value: settings.upgrade_search_interval_hours || '12', min: '1', max: '168', step: '1' });
    appendSettingRow(section, { label: 'Candidates per run (1-200)', key: 'upgrade_search_per_run', value: settings.upgrade_search_per_run || '20', min: '1', max: '200', step: '1' });
    return section;
  }

  // "Probably inactive" settings (v0.148.0): the review list itself is on the Watchlist page, its two settings are here.
  function buildInactiveSettingsSection(settings) {
    const section = window.SRW.el('div', { className: 'srw-settings-section' }, [
      window.SRW.el('h4', { textContent: 'Probably inactive (Watchlist)' }),
    ]);
    appendSettingRow(section, {
      label: 'No new scene for (months)', key: 'inactive_after_months', value: settings.inactive_after_months || '18', min: '1', max: '240', step: '1',
      hint: 'A watchlist entry whose newest scene is older than this is proposed on the Watchlist page > Probably inactive. Nothing is removed automatically.',
    });
    appendSettingRow(section, {
      label: 'Refresh data every (hours)', key: 'inactive_check_interval_hours', value: settings.inactive_check_interval_hours || '168', min: '1', step: '1',
    });
    return section;
  }

  // Jobs tab, read-only: the jobs whose interval is set where their program lives (hybrid rule), with a link to that tab.
  function buildJobsOverviewSection(settings, whisparrConfigured, gotoTab) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Other jobs (set where they belong)' }),
      el('p', { className: 'srw-hint', textContent: 'Intervals of jobs that belong to one program or area are set there. This list only shows them; click a job to jump.' }),
    ]);
    const hours = (key, dflt) => `every ${settings[key] || dflt} h`;
    const rows = [
      ['StashDB favorites sync', hours('stashdb_favorites_sync_interval_hours', '24'), 'connections'],
      ['ThePornDB identify', hours('theporndb_identify_interval_hours', '24'), 'connections'],
      ['File import check', hours('file_import_interval_hours', '0.25'), 'import_naming'],
      ['Torrent check', settings.torrent_health_minutes === '0' ? 'off' : `every ${settings.torrent_health_minutes || 30} min`, 'search_quality'],
      ['Recycle bin cleanup', hours('recycle_bin_cleanup_interval_hours', '24'), 'general'],
          ];
    if (whisparrConfigured) {
      rows.splice(2, 0,
        ['Whisparr safety sweep', hours('safety_sweep_interval_hours', '24'), 'connections'],
        ['Whisparr blocklist sync', hours('whisparr_blocklist_sync_interval_hours', '24'), 'connections'],
        ['Whisparr import status check', hours('import_check_interval_hours', '1'), 'connections']);
    }
    rows.forEach(([name, when, tab]) => {
      section.appendChild(el('div', {}, [
        el('a', { href: '#', textContent: name, onClick: (e) => { e.preventDefault(); gotoTab(tab); } }), ` — ${when}`,
      ]));
    });
    return section;
  }

  function buildScheduleSection(settings) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Watcher Schedule' }),
    ]);
    const scanInput = el('input', { type: 'number', min: '0.5', step: '0.5', value: settings.scan_interval_hours || '6' });
    section.appendChild(el('label', {}, ['Scan interval (hours): ', scanInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'scan_interval_hours', value: scanInput.value });
      },
    }));

    // Scan method (2026-09-25) -- see routers/settings.py's scan_mode.
    const scanModeSelect = el('select', {}, [
      el('option', { value: 'feed', textContent: 'New-releases feed (fast, a few requests per provider)' }),
      el('option', { value: 'per_entry', textContent: 'Per watchlist entry (original, one request per entry)' }),
    ]);
    scanModeSelect.value = settings.scan_mode || 'feed';
    const scanModeStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['Scan method: ', scanModeSelect]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'scan_mode', value: scanModeSelect.value });
        scanModeStatus.textContent = result.error ? ` error: ${result.error}` : ' saved';
      },
    }));
    section.appendChild(scanModeStatus);

    // Milestone 3 (2026-09-19): how often the periodic indexer re-scan
    // loop retries a 'sent_to_whisparr' release that hasn't been grabbed
    // yet -- the one-shot attempt at hand-off time already covers most
    // cases, this just catches whatever it missed. See indexer_search.py.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Indexer search re-scan' }));
    const indexerSearchInput = el('input', {
      type: 'number', min: '0.5', step: '0.5', value: settings.indexer_search_interval_hours || '2',
    });
    section.appendChild(el('label', {}, ['Interval (hours): ', indexerSearchInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', {
          key: 'indexer_search_interval_hours', value: indexerSearchInput.value,
        });
      },
    }));
    appendSettingRow(section, {
      label: 'Search mode', key: 'search_mode', value: settings.search_mode || 'polling',
      options: [['polling', 'Polling (own queries per release)'], ['rss', 'RSS (one feed pull per run, searched locally)']],
      hint: 'RSS keeps the number of indexer requests low: one blank-query feed pull per run, matched locally. Polling asks the indexers for every wanted release.',
    });
    appendSettingRow(section, {
      label: 'RSS pull every (minutes, 10-1440)', key: 'rss_interval_minutes', value: settings.rss_interval_minutes || '30', min: '10', max: '1440', step: '5',
      hint: 'Only used in RSS mode. Not below 10: NZBFinder bans an RSS fetch interval under 10 minutes, and the feed is cached there anyway, so 30 is plenty.',
    });
    appendCategoryRows(section, settings);
    const indexerSearchStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('br'));
    section.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Search now',
      onClick: async (e) => {
        e.target.disabled = true;
        window.SRW.setBusy(indexerSearchStatus, 'searching...');
        try {
          const result = await window.SRW.runOp('indexer_search_run');
          if (result.error) {
            indexerSearchStatus.textContent = ` error: ${result.error}`;
          } else if (result.skipped) {
            indexerSearchStatus.textContent = ` ${result.reason}`;
          } else {
            indexerSearchStatus.textContent = ` done — ${(result.grabbed || []).length} release(s) grabbed`;
          }
        } catch (err) {
          indexerSearchStatus.textContent = ` error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    section.appendChild(indexerSearchStatus);

    // v0.128.0: automatic search for the movies on the Movies wishlist (api/film_search.py). It only PROPOSES (Activity > Needs review),
    // it never grabs by itself. Every movie is asked for at most every N days, M movies per run.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Movie search (Movies wishlist)' }));
    const filmEnabled = el('input', { type: 'checkbox' });
    filmEnabled.checked = String(settings.film_search_enabled || 'false') === 'true';
    const filmInterval = el('input', { type: 'number', min: '1', step: '1', value: settings.film_search_interval_hours || '6' });
    const filmPerRun = el('input', { type: 'number', min: '1', max: '50', step: '1', value: settings.film_search_per_run || '5' });
    const filmDays = el('input', { type: 'number', min: '1', step: '1', value: settings.film_search_min_days || '7' });
    const filmRecentDays = el('input', { type: 'number', min: '1', step: '1', value: settings.film_search_recent_days || '60' });
    const filmRecentMin = el('input', { type: 'number', min: '1', step: '1', value: settings.film_search_recent_min_days || '1' });
    const filmStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('p', { className: 'srw-hint', textContent: 'Searches the indexers for the movies on the Movies wishlist (1-3 requests per movie) and proposes the best plausible release in Activity > Needs review. Nothing is downloaded without your Accept. Where Whisparr searches (Settings > General > Workflow: “Whisparr searches + downloads, this sidecar imports” or “Full Whisparr management”) the movies registered in Whisparr are left to Whisparr; this search then covers only the others.' }));
    section.appendChild(el('label', {}, [filmEnabled, ' Search automatically']));
    section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['Run every (hours): ', filmInterval]));
    section.appendChild(el('label', {}, [' Movies per run: ', filmPerRun]));
    section.appendChild(el('label', {}, [' Ask the same movie again after (days): ', filmDays]));
    section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['New movies (released within the last ', filmRecentDays, ' days or still to come) are asked again after (days): ', filmRecentMin]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const pairs = [['film_search_enabled', filmEnabled.checked ? 'true' : 'false'], ['film_search_interval_hours', filmInterval.value],
          ['film_search_per_run', filmPerRun.value], ['film_search_min_days', filmDays.value],
          ['film_search_recent_days', filmRecentDays.value], ['film_search_recent_min_days', filmRecentMin.value]];
        for (const [key, value] of pairs) {
          const r = await window.SRW.runOp('settings_set', { key, value });
          if (r && r.error) { filmStatus.textContent = ` error: ${r.error}`; return; }
        }
        filmStatus.textContent = ' saved';
      },
    }));
    section.appendChild(filmStatus);

    // v0.131.0: new movies of the watched ThePornDB studios -> Movies wishlist > Suggestions (api/film_feed.py). ONE feed request per run.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'New movies of watched studios (Movies wishlist > Suggestions)' }));
    const feedInterval = el('input', { type: 'number', min: '1', step: '1', value: settings.film_feed_interval_hours || '6' });
    const feedStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('p', { className: 'srw-hint', textContent: 'Reads ThePornDB\'s feed of newly added movies (normally ONE request per run) and puts the movies of your directly watched studios into the Suggestions. Studios on the "Probably inactive" list and VR studios are left out, known and dismissed movies are not suggested again. Nothing is searched or downloaded before you accept a suggestion. The first run only sets the starting point (no back catalogue). 999 = paused.' }));
    section.appendChild(el('label', {}, ['Run every (hours): ', feedInterval]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const r = await window.SRW.runOp('settings_set', { key: 'film_feed_interval_hours', value: feedInterval.value });
        feedStatus.textContent = r && r.error ? ` error: ${r.error}` : ' saved';
      },
    }));
    section.appendChild(feedStatus);

    return section;
  }

  // Settings that only concern the part switched off in Settings > Content are hidden (v0.139.1): a section's own default kind, and the kind of each
  // sub-section (the run of controls after a sub-heading). kind: 'scenes' | 'movies' | null (= used by both, always shown).
  const SECTION_KIND = {
    'Hidden studios': 'scenes', 'Watcher Schedule': 'scenes', Fingerprints: 'scenes', 'Indexer search re-scan': 'scenes', 'Upgrade search': 'scenes',
    'Movie search (Movies wishlist)': 'movies', 'New movies of watched studios (Movies wishlist > Suggestions)': 'movies',
  };
  // Whisparr, Quality Profiles, Naming, the Releases page limit and ThePornDB identify (it is what gives the watched studios their ThePornDB id,
  // which the movie feed matches on) serve both parts. The scene scan, the periodic scene search and the StashDB fingerprint upload are scenes
  // only: movies have their own jobs (the movie feed and the movie search).
  const SUB_KIND = [
    ['Scan', 'scenes'], ['Fingerprints', 'scenes'], ['Indexer search re-scan', 'scenes'],
    ['Movie search', 'movies'], ['New movies of watched studios', 'movies'],
  ];
  function applyContentScope(panels, scope) {
    if (scope === 'both') return;
    const hiddenKind = scope === 'scenes' ? 'movies' : 'scenes'; // the kind that is switched off
    Object.values(panels).forEach((panel) => {
      panel.querySelectorAll('.srw-settings-section').forEach((section) => {
        const h4 = section.querySelector(':scope > h4');
        const defaultKind = SECTION_KIND[h4 ? h4.textContent : ''] || null;
        let kind = defaultKind;
        let shown = 0;
        [...section.children].forEach((child, i) => {
          if (child.classList && child.classList.contains('srw-subsection-heading')) {
            const hit = SUB_KIND.find(([prefix]) => child.textContent.startsWith(prefix));
            kind = hit ? hit[1] : defaultKind;
          }
          if (i === 0 && child === h4) return; // the heading itself decides below
          // A block that carries its own sub-heading inside (e.g. "Whisparr scope") has that heading's kind, the running kind stays as it was.
          const inner = !(child.classList && child.classList.contains('srw-subsection-heading')) && child.querySelector
            ? child.querySelector('.srw-subsection-heading') : null;
          let own = kind;
          if (inner) { const hit = SUB_KIND.find(([prefix]) => inner.textContent.startsWith(prefix)); if (hit) own = hit[1]; }
          const hide = own === hiddenKind;
          child.hidden = hide;
          if (!hide) shown += 1;
        });
        if (h4 && !shown && defaultKind === hiddenKind) section.hidden = true; // nothing left that applies: the whole section goes
      });
    });
  }

  // Settings > Content (v0.139.0): what the plugin and the sidecar look after -- scenes, movies/groups or both. Hides the other area in the plugin
  // (Releases page, Maintenance areas, the Releases tab on studio/performer pages, the group page tab) and keeps its background work from running
  // (scene scan, searches, upgrades, duplicate/identify jobs -- or the movie feed and the movie search). The page reloads to apply it.
  function buildContentScopeSection(settings) {
    const { el } = window.SRW;
    const current = ['scenes', 'movies'].includes(settings.content_scope) ? settings.content_scope : 'both';
    const status = el('span', { className: 'srw-key-status' });
    // Two checkboxes (v0.143.0) instead of Scenes | Movies | Both; at least one stays on. Stored as before: scenes / movies / both.
    const boxes = {};
    const OPTIONS = [['scenes', 'Scenes'], ['movies', 'Movies / Groups']];
    const rows = OPTIONS.map(([key, label]) => {
      const input = el('input', { type: 'checkbox' });
      input.checked = current === 'both' || current === key;
      boxes[key] = input;
      input.addEventListener('change', async () => {
        if (!boxes.scenes.checked && !boxes.movies.checked) { input.checked = true; status.textContent = ' at least one must stay on'; return; }
        const value = boxes.scenes.checked && boxes.movies.checked ? 'both' : (boxes.scenes.checked ? 'scenes' : 'movies');
        Object.values(boxes).forEach((b) => { b.disabled = true; });
        const res = await window.SRW.runOp('settings_set', { key: 'content_scope', value });
        if (res && res.error) {
          status.textContent = ` error: ${res.error}`;
          input.checked = !input.checked; Object.values(boxes).forEach((b) => { b.disabled = false; }); return;
        }
        window.SRW.resetContentScope();
        status.textContent = ' saved -- reloading…';
        setTimeout(() => window.location.reload(), 700);
      });
      return el('label', { style: 'display:block;margin:4px 0' }, [input, ` ${label}`]);
    });
    return el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Content' }),
      el('p', { className: 'srw-hint', textContent: 'What this plugin and the sidecar look after. “Scenes”: new scene releases of your watched studios and performers (calendar, wishlist, searches, upgrades). '
        + '“Movies / Groups”: new movies of your watched studios (suggestions, wishlist, movie search, groups). The area that is switched off disappears from the Releases page, Maintenance and the studio / performer / group pages, and its background jobs stop; '
        + 'downloads that are already running are still imported. Watching a studio works for both. Settings that only concern the switched-off part are hidden.' }),
      ...rows,
      status,
    ]);
  }

  // Milestone 2 (2026-09-17): this sidecar imports/renames/moves a
  // completed download itself instead of trusting Whisparr's own
  // auto-import -- see api/file_importer.py. The two format strings
  // default to whatever Whisparr's own Media Management page had
  // configured at the time this shipped (routers/settings.py's
  // DEFAULTS), so the visible naming doesn't change on cutover; edit
  // them here to change it going forward.
  //
  // Which SOURCE that completed download comes from is now a real
  // switch (added 2026-09-19, see routers/settings.py's
  // Whisparr-scope preset (2026-09-22) -- how much of search/grab/file-
  // import stays with Whisparr vs. this sidecar. Replaces the old
  // standalone `file_import_source` dropdown (this used to just be
  // "Source" above): that setting is now DERIVED from the preset
  // (bookkeeping_only/search_sidecar_import -> sidecar_sabnzbd,
  // full_whisparr -> whisparr_built_in) instead of being an
  // independently-editable control, so the two can no longer drift out
  // of sync. See api/routers/settings.py's WHISPARR_SCOPE_PRESETS and
  // api/handoff.py's send_to_whisparr() for what each value actually
  // changes. Only 3 of the user's own 5 designed presets are buildable
  // today -- "fully standalone" and "Prowlarr-direct search" both
  // depend on work not built yet (direct indexer config, Prowlarr
  // search integration) and aren't offered here.
  // `onSourceChange(fileImportSource)` (kept from the old switch) --
  // fired immediately on every select change with the DERIVED
  // file_import_source value, so buildFileImportSection()'s
  // downloads-mount fields (only meaningful for 'sidecar_sabnzbd') keep
  // reacting live without needing a page reload.
  const WHISPARR_SCOPE_PRESET_OPTIONS = [
    { value: 'bookkeeping_only', label: 'Whisparr informed only (bookkeeping) -- recommended', fileImportSource: 'sidecar_sabnzbd' },
    { value: 'search_sidecar_import', label: 'Whisparr searches + downloads, this sidecar imports', fileImportSource: 'sidecar_sabnzbd' },
    { value: 'full_whisparr', label: 'Full Whisparr management (search, download, import)', fileImportSource: 'whisparr_built_in' },
    // 2026-09-25: Whisparr not used at all. ThePornDB/other providers
    // skip Whisparr under every preset except full_whisparr anyway.
    { value: 'no_whisparr', label: 'No Whisparr (this sidecar searches, downloads, imports)', fileImportSource: 'sidecar_sabnzbd' },
  ];

  function buildWhisparrScopeSection(settings, onSourceChange) {
    const { el } = window.SRW;
    const wrap = el('div');
    wrap.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'How much of search, download and file import stays with Whisparr vs. this sidecar.',
    }));
    const select = el('select', {}, WHISPARR_SCOPE_PRESET_OPTIONS.map(
      (opt) => el('option', { value: opt.value, textContent: opt.label })
    ));
    const currentPreset = settings.whisparr_scope_preset || 'bookkeeping_only';
    select.value = currentPreset;
    const optionFor = (value) => WHISPARR_SCOPE_PRESET_OPTIONS.find((opt) => opt.value === value);
    const SCOPE_NOTES = {
      no_whisparr: 'Whisparr is not used at all: this program searches, downloads and imports.',
      search_sidecar_import: 'Whisparr searches and downloads on its own, for scenes and for the movies registered there. If it finds nothing, this program does not search on '
        + '(not via Prowlarr either) and only imports what Whisparr grabbed. For a fallback through Prowlarr / your indexers choose “Whisparr informed only (bookkeeping)”. '
        + 'ThePornDB scenes, and movies that are not in Whisparr (no ThePornDB id), are still searched by this program.',
      full_whisparr: 'Whisparr does everything: searching, downloading and importing, for scenes and for the movies registered there (this program then only builds the Stash Group of an imported movie). '
        + 'If it finds nothing, nothing else searches (not via Prowlarr either); for a fallback choose “Whisparr informed only (bookkeeping)”. '
        + 'Only StashDB scenes: ThePornDB scenes cannot be handled in this mode. Movies that are not in Whisparr (no ThePornDB id) are still searched by this program.',
    };
    const noteLine = el('p', { className: 'srw-hint' });
    const showNote = () => { noteLine.textContent = SCOPE_NOTES[select.value] || ''; noteLine.hidden = !SCOPE_NOTES[select.value]; };
    showNote();
    select.addEventListener('change', () => { showNote(); onSourceChange(optionFor(select.value).fileImportSource, select.value); });
    const statusLine = el('p', { className: 'srw-hint' });

    async function refreshStatus() {
      try {
        const status = await window.SRW.runOp('file_import_source_status');
        if (status.error) {
          statusLine.textContent = `Couldn’t read Whisparr’s own Completed Download Handling state: ${status.error}`;
          return;
        }
        const cdhText = status.whisparr_cdh_enabled ? 'ON' : 'OFF';
        if (status.in_sync) {
          statusLine.textContent = `In sync -- Whisparr’s own Completed Download Handling is ${cdhText}, as expected for this preset.`;
        } else {
          statusLine.textContent = `Out of sync -- Whisparr’s own Completed Download Handling is ${cdhText}, `
            + 'which doesn’t match the selected preset. Click "Apply to Whisparr" below.';
        }
      } catch (e) {
        statusLine.textContent = `Error checking sync state: ${e.message}`;
      }
    }

    wrap.appendChild(el('label', {}, ['Preset: ', select]));
    wrap.appendChild(noteLine);
    const saveStatus = el('span', { className: 'srw-key-status' });
    wrap.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const fileImportSource = optionFor(select.value).fileImportSource;
        await window.SRW.runOp('settings_set', { key: 'whisparr_scope_preset', value: select.value });
        await window.SRW.runOp('settings_set', { key: 'file_import_source', value: fileImportSource });
        await window.SRW.runOp('file_import_sync_source');
        saveStatus.textContent = ' saved';
        setTimeout(() => { saveStatus.textContent = ''; }, 2000);
        refreshStatus();
      },
    }));
    wrap.appendChild(saveStatus);
    wrap.appendChild(el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Apply to Whisparr',
      onClick: async (e) => {
        e.target.disabled = true;
        statusLine.textContent = ' applying...';
        try {
          const result = await window.SRW.runOp('file_import_sync_source');
          if (result.error) statusLine.textContent = `Error: ${result.error}`;
          else await refreshStatus();
        } catch (err) {
          statusLine.textContent = `Error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    }));
    wrap.appendChild(el('br'));
    wrap.appendChild(statusLine);
    refreshStatus();
    return wrap;
  }

  function buildFileImportSection(settings) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'File Import' }),
    ]);

    // Naming/Permissions moved to their own "File Naming" tab
    // (2026-09-21, user request) -- see buildFileNamingSection().

    const downloadsMountWrap = el('div');
    downloadsMountWrap.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'This sidecar’s own downloads mount' }));
    downloadsMountWrap.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Translates SABnzbd’s own reported path for a completed job to where this sidecar can '
        + 'actually reach the file.',
    }));
    buildConnectionField(downloadsMountWrap, {
      label: 'SABnzbd’s own downloads path', key: 'sabnzbd_downloads_root_path',
      value: settings.sabnzbd_downloads_root_path, placeholder: '/downloads',
    });
    buildConnectionField(downloadsMountWrap, {
      label: 'This sidecar’s local mount of it', key: 'local_downloads_mount',
      value: settings.local_downloads_mount, placeholder: '/downloads',
    });
    // Only meaningful for the 'sidecar_sabnzbd' source -- previously
    // always shown regardless (a real bug the user caught: the hint
    // text itself said "only used when..." but nothing actually hid
    // it). Reacts live to the select above via onSourceChange, not just
    // after Save/reload.
    downloadsMountWrap.hidden = (settings.file_import_source || 'sidecar_sabnzbd') !== 'sidecar_sabnzbd';
    // The Whisparr scope switch lives in its own block (General area, v0.144.0); it calls this when the derived source changes.
    section.applySource = (fileImportSource) => { downloadsMountWrap.hidden = fileImportSource !== 'sidecar_sabnzbd'; };
    section.appendChild(downloadsMountWrap);

    const intervalInput = el('input', { type: 'number', min: '0.1', step: '0.1', value: settings.file_import_interval_hours || '0.25' });
    section.appendChild(el('label', {}, ['Check interval (hours): ', intervalInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'file_import_interval_hours', value: intervalInput.value });
      },
    }));

    // Fingerprint upload (v0.106.7): off by default -- a submission is public at StashDB and cannot be undone.
    const uploadBox = el('input', { type: 'checkbox' });
    uploadBox.checked = settings.fingerprint_upload_enabled === 'true';
    const uploadStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Fingerprints' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'When a downloaded file is rejected by the fingerprint check but you looked at it and it IS the scene, Activity > Import failed can '
        + 'also submit the file\'s fingerprints to StashDB ("Correct + submit to StashDB", with your Stash\'s StashDB account, like the tagger\'s '
        + '"Submit fingerprints"). It is public and cannot be undone, so it is off until you allow it. Without it that button is locked.',
    }));
    section.appendChild(el('label', {}, [uploadBox, ' Allow submitting fingerprints to StashDB']));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const value = uploadBox.checked ? 'true' : 'false';
        const result = await window.SRW.runOp('settings_set', { key: 'fingerprint_upload_enabled', value });
        if (result && result.error) { uploadStatus.textContent = ` error: ${result.error}`; return; }
        settings.fingerprint_upload_enabled = value;
        uploadStatus.textContent = ' saved';
        setTimeout(() => { uploadStatus.textContent = ''; }, 2000);
      },
    }));
    section.appendChild(uploadStatus);

    const runBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Run file import now' });
    runBtn.addEventListener('click', async () => {
      runBtn.disabled = true;
      runBtn.textContent = 'Importing...';
      try {
        const result = await window.SRW.runOp('file_import_run');
        window.SRW.notify('File import', `Checked ${result.checked ?? 0}: ${(result.imported || []).length} imported, `
          + `${(result.import_failed || []).length} failed.`, 'info');
      } catch (e) {
        window.SRW.notify('File import', `File import failed: ${e.message}`, 'error');
      } finally {
        runBtn.disabled = false;
        runBtn.textContent = 'Run file import now';
      }
    });
    section.appendChild(el('div', { className: 'srw-toolbar' }, [runBtn]));

    // Added 2026-09-19 directly in response to that same day's real
    // data-loss bug (see PROGRESS.md's "Milestone 2 cutover: RESOLVED"
    // entry) -- a file this import pipeline is about to replace is now
    // moved here instead of silently overwritten (see
    // file_importer._recycle_existing_target()). Path is relative to
    // the library mount above, the user's own choice (2026-09-19): no
    // extra mount needed, keeps a same-filesystem hardlink possible.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Recycle bin' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'A file this pipeline is about to replace is moved here first, instead of being '
        + 'overwritten outright — the safety net the 2026-09-19 data-loss incident didn’t have.',
    }));
    const recycleBinPathInput = el('input', { type: 'text', value: settings.recycle_bin_path || '.recycle-bin' });
    section.appendChild(el('label', {}, ['Path (relative to the library mount): ', recycleBinPathInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'recycle_bin_path', value: recycleBinPathInput.value });
      },
    }));
    section.appendChild(el('br'));

    const cleanupDaysInput = el('input', {
      type: 'number', min: '0', step: '1', value: settings.recycle_bin_cleanup_days || '7',
    });
    section.appendChild(el('label', {}, ['Delete after (days): ', cleanupDaysInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'recycle_bin_cleanup_days', value: cleanupDaysInput.value });
      },
    }));

    const cleanupIntervalInput = el('input', {
      type: 'number', min: '1', step: '1', value: settings.recycle_bin_cleanup_interval_hours || '24',
    });
    section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['Check interval (hours): ', cleanupIntervalInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', {
          key: 'recycle_bin_cleanup_interval_hours', value: cleanupIntervalInput.value,
        });
      },
    }));

    const cleanupStatus = el('span', { className: 'srw-key-status' });
    const cleanupBtn = el('button', {
      className: 'srw-btn srw-btn-primary', textContent: 'Clean up now',
      onClick: async (e) => {
        e.target.disabled = true;
        cleanupStatus.textContent = ' cleaning up...';
        try {
          const result = await window.SRW.runOp('recycle_bin_cleanup_run');
          if (result.error) cleanupStatus.textContent = ` error: ${result.error}`;
          else cleanupStatus.textContent = ` done — checked ${result.checked ?? 0}, `
            + `${(result.deleted || []).length} permanently deleted`;
        } catch (err) {
          cleanupStatus.textContent = ` error: ${err.message}`;
        } finally {
          e.target.disabled = false;
        }
      },
    });
    section.appendChild(el('br'));
    section.appendChild(el('div', { className: 'srw-toolbar' }, [cleanupBtn, cleanupStatus]));

    // History/Events log retention (2026-09-28, v0.79.0) -- see the
    // History page; pruned once a day.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'History log' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Entries on the History page older than this are deleted once a day (0 = keep everything).',
    }));
    const historyDaysInput = el('input', {
      type: 'number', min: '0', step: '1', value: settings.history_retention_days || '14',
    });
    const historyStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {}, ['Keep entries for (days): ', historyDaysInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'history_retention_days', value: historyDaysInput.value });
        historyStatus.textContent = result && result.error ? ` error: ${result.error}` : ' saved';
        setTimeout(() => { historyStatus.textContent = ''; }, 2000);
      },
    }));
    section.appendChild(historyStatus);

    // SABnzbd's Archive (v0.222.3): its duplicate detection reads it, so an old entry can block a re-grab with "Duplicate NZB"
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'SABnzbd archive' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'SABnzbd 4.2+ keeps deleted history entries in its Archive, and its duplicate detection reads them. Entries older than this are removed from the Archive once a day (only the category of the download client, when one is set; 0 = never).',
    }));
    const archiveDaysInput = el('input', {
      type: 'number', min: '0', step: '1', value: settings.sab_archive_purge_days || '30',
    });
    const archiveStatus = el('span', { className: 'srw-key-status' });
    section.appendChild(el('label', {}, ['Remove entries older than (days): ', archiveDaysInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        const result = await window.SRW.runOp('settings_set', { key: 'sab_archive_purge_days', value: archiveDaysInput.value });
        archiveStatus.textContent = result && result.error ? ` error: ${result.error}` : ' saved';
        setTimeout(() => { archiveStatus.textContent = ''; }, 2000);
      },
    }));
    section.appendChild(archiveStatus);

    return section;
  }

  // Every token file_naming.py's render() actually substitutes (see its
  // own build_tokens()) -- 2026-09-21, user request: the Naming section
  // only ever showed 4 of the 7 real tokens (via whatever happened to
  // be in the two default format strings), with no documentation of
  // the rest anywhere in this UI. Kept as one flat list here rather
  // than fetched from the backend -- this vocabulary is a fixed part of
  // file_naming.py's own code, not configurable data.
  const NAMING_TOKENS = [
    { token: 'Scene Title', desc: 'The scene\'s own title.' },
    { token: 'Studio Title', desc: 'The scene\'s studio name.' },
    { token: 'Release Date', desc: 'The scene\'s release date (YYYY-MM-DD).' },
    { token: 'Quality Full', desc: 'Resolution/source description, e.g. "WEBDL-1080p".' },
    { token: 'Release Year', desc: 'The year of the release date (2026).' },
    { token: 'Source', desc: 'The source part of the quality ("WEBDL").' },
    { token: 'Resolution', desc: 'The resolution part of the quality ("1080p", VR: the width class).' },
    { token: 'Tag Folder', desc: 'The folder of the FIRST rule in "Tag folders" below that matches one of the scene\'s tags; empty if none does.' },
    { token: 'Performers Female', desc: 'Comma-separated female performer names.' },
    { token: 'Performers Male', desc: 'Comma-separated male performer names.' },
    { token: 'Performers Other', desc: 'Comma-separated performers of any other/unspecified gender.' },
  ];

  const MOVIE_NAMING_TOKENS = [
    { token: 'Movie Title', desc: 'The film\'s title (the group\'s name without a trailing year; "Foo, The" becomes "The Foo").' },
    { token: 'Movie Year', desc: 'The release year (from the "(YYYY)" of the name or the date); empty when unknown.' },
    { token: 'Part Number', desc: 'The scene number of a film with several scenes or discs ("2"); empty for a one-file film.' },
    { token: 'Studio Title', desc: 'The studio of the film, when known.' },
    { token: 'Quality Full', desc: 'Source and resolution, e.g. "WEBDL-1080p".' },
    { token: 'Source', desc: 'The source part of the quality ("WEBDL").' },
    { token: 'Resolution', desc: 'The resolution part of the quality ("1080p").' },
  ];

  // Naming + Permissions (2026-09-21, user request) -- split out of the
  // old combined "Schedule & Import" tab's "File Import" box into their
  // own tab: naming needed real documentation (see NAMING_TOKENS above)
  // that didn't fit as a one-line hint next to two bare text inputs,
  // and grouping it with Permissions (both are "how a file ends up on
  // disk" concerns, unlike the rest of File Import's scheduling/source/
  // recycle-bin settings) reads more coherently on its own page.
  // "Organized" hook (v0.191.0, own switch since v0.198.0): the mark in Stash as the trigger. A deliberate decision: off until switched on here.
  function buildOrganizedHookSection(settings) {
    const { el } = window.SRW;
    const initial = settings.organized_hook || 'off';
    const sw = el('input', { type: 'checkbox' });
    sw.checked = initial !== 'off';
    const mode = el('select', {}, [el('option', { value: 'preview', textContent: 'Preview only (writes the planned move to History, moves nothing)' }),
      el('option', { value: 'on', textContent: 'Move for real' })]);
    mode.value = initial === 'on' ? 'on' : 'preview';
    const state = el('span', { className: 'srw-key-status' });
    const modeRow = el('div', { className: 'srw-toolbar' }, [el('span', { textContent: 'Mode: ' }), mode]);
    const show = () => {
      modeRow.hidden = !sw.checked;
      state.textContent = !sw.checked ? ' off' : (mode.value === 'on' ? ' ON: scenes you mark Organized are moved' : ' ON, preview only: nothing is moved');
    };
    const save = async () => {
      const value = !sw.checked ? 'off' : mode.value;
      const r = await window.SRW.runOp('settings_set', { key: 'organized_hook', value });
      if (r.error) { window.SRW.notify('Organized hook', r.error, 'error'); return; }
      settings.organized_hook = value;
      show();
    };
    sw.addEventListener('change', save);
    mode.addEventListener('change', save);
    show();
    return el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Organized hook' }),
      el('p', { className: 'srw-hint', textContent: 'Off by default -- you decide whether to use it. When it is on, marking a scene Organized in Stash moves/renames it: a movie scene (in ONE group of Stash) '
        + 'goes to the movie template (Naming > Movies/Groups), a scene in an import source folder with a matching provider id goes to the naming scheme -- the same plan as Maintenance > Import folder, '
        + 'for that one scene, and a scene with a provider id in the library folder scenes/ is brought to the naming scheme the way Organize library does it (fingerprint-checked, title and date from the provider, the studio in its provider spelling). Other scenes are left alone and History says why. Start with Preview. While another plugin that renames files (renamerOnUpdate ...) is enabled in Stash '
        + 'the hook is held back, see the Safety check above. The plugin has to be reloaded once for Stash to know the hook.' }),
      el('label', {}, [sw, ' Move / rename a scene when I mark it Organized in Stash']), state, modeRow,
    ]);
  }

  function buildFileNamingSection(settings) {
    const { el } = window.SRW;
    const section = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Naming' }),
    ]);

    const tokenList = el('ul', { className: 'srw-naming-token-list' },
      NAMING_TOKENS.map((t) => el('li', {}, [
        el('code', { textContent: `{${t.token}}` }),
        el('span', { textContent: ` — ${t.desc}` }),
      ])));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Available tokens for both format fields below (mirrors Whisparr\'s own scene-naming '
        + 'tokens, so an existing format string produces the same filenames):',
    }));
    section.appendChild(tokenList);

    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'Optional part: put it in < > -- it is used only when EVERY token inside has a value, otherwise it disappears whole, separators included. '
        + 'Example: {Scene Title}< - {Release Date}> {Quality Full} gives "My Scene WEBDL-1080p" for a scene without a date. '
        + 'In the folder format a path segment can be just an optional part: scenes/<{Tag Folder}>/{Studio Title}/… (an optional part must stay inside one segment, not span a "/"). '
        + 'Illegal characters of file names are replaced by a space; a name longer than the file system allows has its title shortened.',
    }));
    const sceneFormatInput = el('input', { type: 'text', style: 'width: 100%;', value: settings.scene_naming_format || '' });
    section.appendChild(el('label', {}, ['Scene Format: ', sceneFormatInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'scene_naming_format', value: sceneFormatInput.value });
      },
    }));

    const folderFormatInput = el('input', { type: 'text', style: 'width: 100%;', value: settings.scene_folder_format || '' });
    section.appendChild(el('label', {}, ['Scene Folder Format: ', folderFormatInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'scene_folder_format', value: folderFormatInput.value });
      },
    }));

    // Movies (v0.194.0): the same template language for films -- downloads of the Movies wishlist, Import folder / "Organized" hook for grouped scenes.
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Movies/Groups' }));
    section.appendChild(el('p', {
      className: 'srw-hint',
      textContent: 'The same rules as above (optional parts < >, replace words), with these tokens. The movie folder format starts with the library folder for films '
        + '(default Movies/…); the first folder is also where Maintenance > Movies & groups looks for films. The defaults give Movies/<Title> (<Year>)/<Title> (<Year>)[ - part N] <Quality>.',
    }));
    section.appendChild(el('ul', { className: 'srw-naming-token-list' }, MOVIE_NAMING_TOKENS.map((t) => el('li', {}, [
      el('code', { textContent: `{${t.token}}` }), el('span', { textContent: ` — ${t.desc}` })]))));
    const movieFolderInput = el('input', { type: 'text', style: 'width: 100%;', value: settings.movie_folder_format || 'Movies/{Movie Title}< ({Movie Year})>' });
    const movieFileInput = el('input', { type: 'text', style: 'width: 100%;', value: settings.movie_naming_format || '{Movie Title}< ({Movie Year})>< - part {Part Number}> {Quality Full}' });
    const saveMovieFormat = (key, input) => async (e) => {
      const r = await window.SRW.runOp('settings_set', { key, value: input.value });
      if (r.error) window.SRW.notify('Naming', r.error, 'error'); else window.SRW.notify('Naming', 'Saved.', 'success');
    };
    section.appendChild(el('label', {}, ['Movie Folder Format: ', movieFolderInput]));
    section.appendChild(el('button', { className: 'srw-btn', textContent: 'Save', onClick: saveMovieFormat('movie_folder_format', movieFolderInput) }));
    section.appendChild(el('label', {}, ['Movie File Format: ', movieFileInput]));
    section.appendChild(el('button', { className: 'srw-btn', textContent: 'Save', onClick: saveMovieFormat('movie_naming_format', movieFileInput) }));

    // Example under the format fields (v0.193.0): what the formats, replacement rules and tag folders -- also unsaved edits -- produce. Writes nothing.
    const previewBox = el('div', { className: 'srw-hint', style: 'margin: 0.5rem 0;' });
    let previewTimer = null;
    let previewExtra = {};
    const refreshPreview = () => {
      clearTimeout(previewTimer);
      previewTimer = setTimeout(async () => {
        const r = await window.SRW.runOp('settings_naming_preview', { folder_format: folderFormatInput.value, filename_format: sceneFormatInput.value,
          movie_folder_format: movieFolderInput.value, movie_filename_format: movieFileInput.value, ...previewExtra });
        if (r.error) { previewBox.textContent = `Example not available: ${r.error}`; return; }
        previewBox.replaceChildren(el('div', { textContent: `Example: ${r.example}` }),
          r.example_with_tag ? el('div', { textContent: `Example with the tag "${r.tag}": ${r.example_with_tag}` }) : null,
          el('div', { textContent: `Movie: ${r.movie_example}` }),
          el('div', { textContent: `Movie, scene 2 of several: ${r.movie_example_part}` }),
          el('div', { textContent: `Movie without a year: ${r.movie_example_no_year}` }));
      }, 300);
    };
    sceneFormatInput.addEventListener('input', refreshPreview);
    folderFormatInput.addEventListener('input', refreshPreview);
    movieFolderInput.addEventListener('input', refreshPreview);
    movieFileInput.addEventListener('input', refreshPreview);
    section.appendChild(previewBox);

    // A small ordered rule editor for the two list settings (JSON in the sidecar): rows of text fields, up/down, remove, save.
    function ruleEditor({ title, hint, key, fields, previewKey, addLabel }) {
      let rules = [];
      try { rules = JSON.parse(settings[key] || '[]'); } catch (e) { rules = []; }
      if (!Array.isArray(rules)) rules = [];
      const box = el('div');
      const rows = el('div');
      const status = el('span', { className: 'srw-key-status' });
      const draw = () => {
        rows.replaceChildren(...rules.map((rule, i) => {
          const inputs = fields.map((f) => {
            const control = f.options
              ? el('select', {}, f.options.map((o) => el('option', { value: o, textContent: o })))
              : el('input', { type: 'text', placeholder: f.placeholder, style: `width: ${f.width || '12rem'};` });
            control.value = rule[f.name] || f.default || '';
            control.addEventListener('input', () => { rule[f.name] = control.value; previewExtra = { ...previewExtra, [previewKey]: rules }; refreshPreview(); });
            return control;
          });
          const move = (d) => () => { const j = i + d; if (j < 0 || j >= rules.length) return; [rules[i], rules[j]] = [rules[j], rules[i]]; draw(); previewExtra = { ...previewExtra, [previewKey]: rules }; refreshPreview(); };
          return el('div', { className: 'srw-toolbar' }, [...inputs,
            el('button', { className: 'srw-btn', textContent: '↑', title: 'Up (higher priority / earlier)', onClick: move(-1) }),
            el('button', { className: 'srw-btn', textContent: '↓', title: 'Down', onClick: move(1) }),
            el('button', { className: 'srw-btn', textContent: 'Remove', onClick: () => { rules.splice(i, 1); draw(); previewExtra = { ...previewExtra, [previewKey]: rules }; refreshPreview(); } })]);
        }));
      };
      box.append(el('p', { className: 'srw-subsection-heading', textContent: title }), el('p', { className: 'srw-hint', textContent: hint }), rows,
        el('div', { className: 'srw-toolbar' }, [
          el('button', { className: 'srw-btn', textContent: addLabel, onClick: () => { rules.push({}); draw(); } }),
          el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Save', onClick: async () => {
            const clean = rules.filter((r) => fields.some((f) => String(r[f.name] || '').trim()));
            const r = await window.SRW.runOp('settings_set', { key, value: JSON.stringify(clean) });
            status.textContent = r.error ? ` error: ${r.error}` : ' saved';
            if (!r.error) { rules = clean; settings[key] = JSON.stringify(clean); draw(); }
          } }),
          status]));
      draw();
      return box;
    }
    section.appendChild(ruleEditor({
      title: 'Replace words', key: 'name_replacements', previewKey: 'name_replacements', addLabel: 'Add a rule',
      hint: 'Applied, in this order, to the title, studio, performer names and tag folder before the name is built (not to date or quality). '
        + 'text = anywhere, ignoring case; word = whole words only; regex = a regular expression. Empty "replace with" removes it (e.g. find \' -> Dads Place).',
      fields: [{ name: 'find', placeholder: 'find', width: '14rem' }, { name: 'replace', placeholder: 'replace with', width: '14rem' }, { name: 'mode', options: ['text', 'word', 'regex'], default: 'text' }],
    }));
    section.appendChild(ruleEditor({
      title: 'Tag folders', key: 'tag_folders', previewKey: 'tag_folders', addLabel: 'Add a tag',
      hint: 'Which tags get a folder of their own, for the token {Tag Folder} (e.g. folder format scenes/<{Tag Folder}>/{Studio Title}/…). Only the tags listed here count, '
        + 'and when a scene has several of them the one HIGHEST in this list wins -- so tags never compete by accident. Folder name empty = the tag name. '
        + 'A new format applies to new imports and to Maintenance > Organize library, which then offers the files that would move.',
      fields: [{ name: 'tag', placeholder: 'tag (e.g. VR)', width: '14rem' }, { name: 'folder', placeholder: 'folder name (optional)', width: '14rem' }],
    }));
    refreshPreview();

    const hardlinksInput = el('input', { type: 'checkbox' });
    hardlinksInput.checked = (settings.use_hardlinks || 'true') === 'true';
    const hardlinksLabel = el('label', {}, [hardlinksInput, ' Use hardlinks instead of copy']);
    hardlinksInput.addEventListener('change', async () => {
      await window.SRW.runOp('settings_set', { key: 'use_hardlinks', value: hardlinksInput.checked ? 'true' : 'false' });
    });
    section.appendChild(hardlinksLabel);

    // Whisparr's own Media Management > Permissions section (Set
    // Permissions / chmod Folder / chown Group), added 2026-09-19 --
    // third of the four re-prioritized Milestone 2 file-handling gaps.
    // Off by default, same reasoning as Whisparr's own default -- most
    // setups don't need this. See file_importer._apply_permissions().
    section.appendChild(el('p', { className: 'srw-subsection-heading', textContent: 'Permissions' }));
    const setPermissionsInput = el('input', { type: 'checkbox' });
    setPermissionsInput.checked = (settings.set_permissions || 'false') === 'true';
    const setPermissionsLabel = el('label', {}, [setPermissionsInput, ' Set permissions on import']);
    setPermissionsInput.addEventListener('change', async () => {
      await window.SRW.runOp('settings_set', {
        key: 'set_permissions', value: setPermissionsInput.checked ? 'true' : 'false',
      });
    });
    section.appendChild(setPermissionsLabel);
    section.appendChild(el('br'));

    const chmodInput = el('input', { type: 'text', value: settings.chmod_folder || '755', style: 'width: 5rem;' });
    section.appendChild(el('label', {}, ['Chmod folder: ', chmodInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'chmod_folder', value: chmodInput.value });
      },
    }));

    const chownGroupInput = el('input', { type: 'text', value: settings.chown_group || '', placeholder: '(not set)' });
    section.appendChild(el('br'));
    section.appendChild(el('label', {}, ['Chown group: ', chownGroupInput]));
    section.appendChild(el('button', {
      className: 'srw-btn', textContent: 'Save',
      onClick: async () => {
        await window.SRW.runOp('settings_set', { key: 'chown_group', value: chownGroupInput.value });
      },
    }));

    return section;
  }


  // Same real-world entity can have a watchlist row per provider it's
  // matched against (see db.py's schema docstring) -- both rows always
  // share the exact same `name` (see watchlist_repo.find_by_name()'s
  // docstring), so grouping by entity_type+name merges them back into
  // one display entity without any fuzzy matching. `local_id` (this
  // Stash instance's own numeric id, added 2026-09-12 -- see
  // PROGRESS.md's "Watchlist: merge duplicates, filter, link to Stash"
  // section) should be identical on both rows when present; whichever
  // row has it wins.
  function groupEntries(entries) {
    const byKey = new Map();
    entries.forEach((entry) => {
      const key = `${entry.entity_type}:${entry.name}`;
      let group = byKey.get(key);
      if (!group) {
        group = {
          entity_type: entry.entity_type, name: entry.name, local_id: null, providers: [],
          // Auto hand-off (2026-09-13) only ever applies to the
          // provider='stashdb' row -- see routers/watchlist.py's
          // set_auto_handoff() -- so its state is read from that row
          // specifically rather than merged/guessed across providers.
          stashdbId: null, autoHandoff: false, autoHandoffFrom: null, autoHandoffUntil: null,
        };
        byKey.set(key, group);
      }
      if (entry.local_id) group.local_id = entry.local_id;
      group.providers.push(entry.provider);
      // Date window (2026-09-26): same on every provider row of the entity;
      // any row's (provider, id) identifies it for saving.
      if (!group.anyProvider) { group.anyProvider = entry.provider; group.anyId = entry.stashdb_id; }
      if (entry.auto_handoff_from) group.autoHandoffFrom = entry.auto_handoff_from;
      if (entry.auto_handoff_until) group.autoHandoffUntil = entry.auto_handoff_until;
      if (entry.provider === 'stashdb') {
        group.stashdbId = entry.stashdb_id;
        group.autoHandoff = !!entry.auto_handoff;
        group.autoHandoffFrom = entry.auto_handoff_from || null;
        group.autoHandoffUntil = entry.auto_handoff_until || null;
      }
    });
    return Array.from(byKey.values());
  }

  // Auto hand-off controls (2026-09-13, see PROGRESS.md's "Auto
  // hand-off to Whisparr, per entity" section) -- a checkbox plus an
  // optional from/until scene-date window, shown per watchlist entity.
  // Deliberately opt-in and per-entity rather than a single global
  // toggle, given the watchlist can run into the thousands. Disabled
  // (not hidden -- so its presence/tooltip still explains why) for an
  // entity with no StashDB-provider row, since hand-off itself only
  // ever works for StashDB-sourced releases.
  let openWindowPop = null; // closes the floating date-window box that is open, so only one is open at a time
  function buildAutoHandoffControls(entity) {
    const { el } = window.SRW;
    const canAutoHandoff = !!entity.stashdbId;

    const checkbox = el('input', { type: 'checkbox', className: 'srw-auto-handoff-checkbox' });
    checkbox.checked = entity.autoHandoff;
    checkbox.disabled = !canAutoHandoff;

    // Date window (2026-09-26): no longer tied to Auto → Wishlist -- it
    // limits everything for this entity (scan, Missing from Catalog,
    // auto), so it's always shown and saved for all its provider rows.
    const fromInput = el('input', { type: 'date', className: 'srw-auto-handoff-date' });
    fromInput.value = entity.autoHandoffFrom || '';
    const untilInput = el('input', { type: 'date', className: 'srw-auto-handoff-date' });
    untilInput.value = entity.autoHandoffUntil || '';
    const windowDisabled = !entity.local_id;
    [fromInput, untilInput].forEach((input) => { input.disabled = windowDisabled; });
    fromInput.placeholder = defaultWindowFrom ? `default ${defaultWindowFrom}` : '';

    // v0.150.0: the two date fields were always on the row and made it wrap. They now sit behind one button that opens a floating box
    // (like the confirm box on a Calendar pill): the window applies to everything for this entity, not only to Auto.
    const windowText = () => {
      const from = entity.autoHandoffFrom || (defaultWindowFrom ? `${defaultWindowFrom} (default)` : '');
      const until = entity.autoHandoffUntil || '';
      return from || until ? `Window: ${from || '…'} → ${until || '…'}` : 'Window: any date';
    };
    const windowBtn = el('button', {
      type: 'button', className: 'srw-btn',
      title: windowDisabled ? 'Available after the next scan links it to its Stash page'
        : 'Date window: only scenes released in this range are stored by the scan, loaded in Missing from Catalog and auto-sent. Click to change.',
    });
    windowBtn.textContent = windowText();
    if (windowDisabled) windowBtn.disabled = true;
    const windowStatus = el('span', { className: 'srw-key-status' });
    let pop = null;
    function closePop() {
      if (!pop) return;
      pop.remove(); pop = null;
      const host = windowWrap.closest('.srw-auto-handoff'); if (host) host.classList.remove('srw-pop-open');
      document.removeEventListener('mousedown', onOutside, true);
      document.removeEventListener('keydown', onKey, true);
    }
    function onOutside(e) { if (pop && !pop.contains(e.target) && e.target !== windowBtn) closePop(); }
    function onKey(e) { if (e.key === 'Escape') closePop(); }
    windowBtn.addEventListener('click', () => {
      if (pop) { closePop(); return; }
      if (openWindowPop) openWindowPop();
      fromInput.value = entity.autoHandoffFrom || '';
      untilInput.value = entity.autoHandoffUntil || '';
      const saveBtn = el('button', { type: 'button', className: 'srw-btn srw-btn-primary', textContent: 'Save' });
      const clearBtn = el('button', { type: 'button', className: 'srw-btn', textContent: 'Clear' });
      const cancelBtn = el('button', { type: 'button', className: 'srw-btn', textContent: 'Cancel' });
      pop = el('div', { className: 'srw-window-pop' }, [
        el('div', { className: 'srw-window-pop-title', textContent: `Date window — ${entity.name}` }),
        el('div', { className: 'srw-window-pop-hint', textContent: 'Limits everything for this entity: which scenes the scan stores, what “Missing from Catalog” loads, '
          + `and Auto → Wishlist.${defaultWindowFrom ? ` From empty = global default ${defaultWindowFrom}.` : ''}` }),
        el('label', {}, ['From ', fromInput]),
        el('label', {}, ['Until ', untilInput]),
        el('div', { className: 'srw-window-pop-actions' }, [saveBtn, clearBtn, cancelBtn]),
      ]);
      windowWrap.appendChild(pop);
      const host = windowWrap.closest('.srw-auto-handoff'); if (host) host.classList.add('srw-pop-open'); // its opacity makes a stacking context: lift it above the rows below
      openWindowPop = closePop;
      document.addEventListener('mousedown', onOutside, true);
      document.addEventListener('keydown', onKey, true);
      saveBtn.addEventListener('click', async () => { await saveWindow(); windowBtn.textContent = windowText(); closePop(); });
      clearBtn.addEventListener('click', async () => { fromInput.value = ''; untilInput.value = ''; await saveWindow(); windowBtn.textContent = windowText(); closePop(); });
      cancelBtn.addEventListener('click', closePop);
    });
    const windowWrap = el('span', { className: 'srw-auto-handoff-dates srw-window-wrap' }, [windowBtn, windowStatus]);
    const dateWrap = windowWrap;

    async function saveWindow() {
      const result = await window.SRW.runOp('backlog_set_window', {
        provider: entity.anyProvider, entity_type: entity.entity_type, entity_stashdb_id: entity.anyId,
        from_date: fromInput.value, until_date: untilInput.value,
      });
      if (result.error) { window.SRW.notify('Date window', `Failed to save: ${result.error}`, 'error'); return; }
      entity.autoHandoffFrom = fromInput.value || null;
      entity.autoHandoffUntil = untilInput.value || null;
      if (result.open_outside > 0) {
        window.SRW.notify('Date window', `${entity.name}: ${result.open_outside} open release(s) lie outside the new `
          + 'window -- dismiss them from its "Missing from Catalog" panel, or leave them.', 'info');
      }
    }

    async function save() {
      const result = await window.SRW.runOp('watchlist_set_auto_handoff', {
        provider: 'stashdb',
        entity_type: entity.entity_type,
        stashdb_id: entity.stashdbId,
        enabled: checkbox.checked,
        from_date: fromInput.value || null,
        until_date: untilInput.value || null,
      });
      if (result.error) window.SRW.notify('Settings', `Failed to save: ${result.error}`, 'error');
    }

    checkbox.addEventListener('change', () => {
      // No confirm() here (removed 2026-09-13) -- it became a hindrance
      // when enabling this for several entities in a row, one popup
      // per click. The warning now lives once, prominently, in the
      // callout above the list (see renderWatchlist()) instead of
      // interrupting every individual checkbox.
      save();
    });

    const label = el('label', {
      className: 'srw-auto-handoff-label',
      title: canAutoHandoff
        ? 'Automatically add new releases from this entity to the wishlist'
        : 'Only available for StashDB-sourced entities (needs a StashDB scene id)',
    }, [checkbox, ' Auto → Wishlist']);

    return el('span', { className: 'srw-auto-handoff' }, [label, dateWrap]);
  }

  // Builds one entity's <li> -- shared by both the letter-grouped view
  // and the flat search-results view below, so the row markup (icon,
  // name/link, provider badges, auto hand-off controls) is defined
  // exactly once.
  function buildEntityLi(entity) {
    const { el } = window.SRW;
    const icon = window.SRW.entityIcon(entity.entity_type);
    const nameNode = entity.local_id
      ? el('a', {
        className: 'srw-watchlist-entity-link',
        href: `/${entity.entity_type}s/${entity.local_id}`,
        textContent: entity.name,
      })
      : el('span', {
        className: 'srw-watchlist-entity-unlinked',
        textContent: entity.name,
        title: 'Will link to its Stash page after the next scan',
      });
    const badges = entity.providers.map((p) => el('span', {
      className: `srw-provider-badge srw-provider-badge-${p}`,
      textContent: window.SRW.providerLabel(p),
    }));
    return el('li', {}, [icon, nameNode, ...badges, buildAutoHandoffControls(entity)]);
  }

  // The Watchlist page (v0.147.0): the list of watched studios/performers plus Probably inactive and Hidden studios as tabs (the Source setting is in Settings > General since v0.148.0).
  // Lives in this file because it shares the builders above; a file of its own is part of the later cleanup.
  async function renderWatchlistPage(container) {
    const { el } = window.SRW;
    container.innerHTML = '';
    container.appendChild(window.SRW.buildPageHeader('watchlist'));
    await window.SRW.loadProviders();
    const [settings, scope] = await Promise.all([window.SRW.runOp('settings_get_all'), window.SRW.getContentScope()]);
    watchlistMode = settings.watchlist_source === 'tag' ? 'tag' : 'favorites';
    watchlistTagName = settings.watchlist_tag_name || 'Watchlist';
    defaultWindowFrom = settings.default_window_from || '';
    const inactivePane = buildInactiveReviewSection(settings);
    inactivePane.open = true; // a tab, not a fold-away section: its heading becomes the tab label (with the count)
    inactivePane.classList.add('srw-panel-flat');
    const sourceText = watchlistMode === 'tag' ? `Stash tag “${watchlistTagName}”` : 'Stash favorites';
    const panes = [
      { key: 'list', label: 'Watchlist', pane: el('div', {}, [
        el('p', { className: 'srw-hint' }, ['Source: ', sourceText, ' · ', el('a', { href: '/plugins/stash-release-manager/settings#general', textContent: 'change in Settings' })]),
        el('div', { className: 'srw-watchlist-wrap' }),
      ]) },
      { key: 'inactive', label: () => inactivePane.querySelector('summary').textContent, pane: inactivePane, watch: inactivePane.querySelector('summary') },
    ];
    // Hidden studios are a scenes-only setting: no tab when only movies are looked after.
    if (scope !== 'movies') panes.push({ key: 'hidden', label: 'Hidden studios', pane: buildHiddenStudiosSection() });
    container.appendChild(window.SRW.buildPaneTabs(panes, 'srw:watchlist-tab'));
    panes.forEach((p) => container.appendChild(p.pane));
    await renderWatchlist(container);
  }

  async function renderWatchlist(container) {
    const { el } = window.SRW;
    const wrap = container.querySelector('.srw-watchlist-wrap');
    wrap.textContent = 'Loading...';
    const entries = await window.SRW.runOp('watchlist_list');
    if (entries.error) { wrap.textContent = `Error: ${entries.error}`; return; }

    const allEntities = groupEntries(entries);
    const PAGE_SIZE = 40; // matches Stash's own default page size (e.g. /studios), 2026-09-13
    let typeFilter = 'both'; // 'both' | 'studio' | 'performer' -- per-render UI state, not persisted
    let searchText = ''; // per-render UI state, not persisted -- same idea as the Calendar's own text filter
    // Pre-selected on open (2026-09-12) rather than starting empty --
    // "A" gives an immediate, orientable view instead of a blank list
    // the user has to click into first. Falls back to the first letter
    // that still has matches (see renderList()) if "A" doesn't under
    // the current type/search filter.
    let activeLetter = 'A';
    let page = 1; // current page within activeLetter's matches, reset on any filter change
    let byLetter = {};
    let letters = [];

    wrap.innerHTML = '';
    wrap.appendChild(el('h4', { textContent: `Watchlist (${entries.length})` }));
    wrap.appendChild(el('p', {
      className: 'srw-hint',
      textContent: (watchlistMode === 'tag'
        ? `Synced automatically from Studios/Performers tagged "${watchlistTagName}" before every scan. `
          + 'Add or remove the tag in Stash (or use the eye icon on its page) to watch or unwatch it. '
        : 'Synced automatically from Stash’s Favorite Studios/Performers before every scan. '
          + 'Star or unstar an entity in Stash itself to watch or unwatch it — nothing to edit here. ')
        + 'Entries matched against both StashDB and ThePornDB are merged into one row here (they still scan and '
        + 'appear as separate release rows elsewhere).',
    }));
    // Split out from the general hint above (2026-09-13) and made
    // visually prominent -- this used to also be a window.confirm()
    // popup on every single enable click, which got in the way when
    // turning it on for several entities in a row. Said once, clearly,
    // here instead.
    wrap.appendChild(el('p', {
      className: 'srw-callout srw-callout-auto',
      textContent: '"Auto → Wishlist" sends every new release from that entity straight to the wishlist on its '
        + 'next scan — no manual confirmation. Off by default. The date window (from/until) limits everything for '
        + 'that entity: which scenes the scan stores, what "Missing from Catalog" loads, and Auto → Wishlist.',
    }));

    // Same filter-bar look as the Calendar view's own text+type filter
    // (2026-09-13, user asked to reuse it here too) -- a text search is
    // far more useful than the A-Z nav alone once the watchlist runs
    // into the thousands, letting you jump straight to one entity by
    // (partial) name instead of picking a letter and scanning by eye.
    // Search only re-renders the current page's content (renderPage()),
    // not the letters themselves -- keeping the A-Z nav's own buttons
    // stable while typing (2026-09-13: recomputing letters from the
    // search-filtered set made buttons disappear/reappear on every
    // keystroke, reported as inconsistent). Search itself still looks
    // across the whole (type-filtered) watchlist, not just whichever
    // letter happens to be selected -- applySearchAutoJump() below
    // moves the highlighted letter to wherever the match actually is,
    // it never removes or greys out the others.
    const searchInput = el('input', {
      type: 'search', placeholder: 'Search studio or performer name…', value: searchText,
      className: 'srw-search-input',
      oninput: (e) => { searchText = e.target.value; page = 1; applySearchAutoJump(); renderPage(); },
    });
    const FILTERS = [
      { key: 'both', label: 'All' },
      { key: 'studio', label: 'Studios' },
      { key: 'performer', label: 'Performer' },
    ];
    const filterButtons = FILTERS.map(({ key, label }) => el('button', {
      className: 'srw-btn srw-btn-sm', textContent: label,
      onClick: () => { typeFilter = key; renderList(); },
    }));
    // A-Z nav lives inside the same filter-bar box, on its own centered
    // line below the search/type row (2026-09-13, previously a
    // separate block below the bar) -- letter is a filter dimension
    // same as type, just derived from the type filter only (not
    // search, see above), so its available letters/counts don't shift
    // while typing.
    const letterNav = el('div', { className: 'srw-watchlist-nav' });
    const filterBar = el('div', { className: 'srw-filter-bar' }, [
      searchInput,
      el('span', { className: 'srw-view-toggle' }, filterButtons),
      letterNav,
    ]);
    const contentArea = el('div');
    wrap.appendChild(filterBar);
    wrap.appendChild(contentArea);

    // Plain <button>, not an <a href="#..."> -- confirmed live
    // 2026-09-12: Stash's own SPA router intercepts anchor-tag clicks
    // globally and navigated to its home dashboard instead of just
    // updating the hash, since "#srw-watchlist-letter-X" isn't one of
    // its own known routes.
    function updateNavActiveHighlight() {
      letterNav.querySelectorAll('.srw-watchlist-nav-link').forEach((btn) => {
        btn.classList.toggle('srw-watchlist-nav-link-active', btn.textContent === activeLetter);
      });
    }

    function letterOf(name) {
      const first = (name || '').trim().charAt(0).toUpperCase();
      return /[A-Z]/.test(first) ? first : '#';
    }

    // Only called when the search text itself changes (2026-09-13) --
    // a manual letter click (showLetter()) never calls this, so
    // explicitly picking a letter with zero current search matches
    // still shows that letter (empty), rather than being immediately
    // overridden back to wherever the match is.
    function applySearchAutoJump() {
      const q = searchText.trim().toLowerCase();
      if (!q) return; // nothing to jump to -- leave activeLetter as last set
      const matches = allEntitiesByType().filter((e) => (e.name || '').toLowerCase().includes(q));
      if (matches.length === 0) return; // no match anywhere; renderPage() reports it on whatever letter is active
      if (matches.some((e) => letterOf(e.name) === activeLetter)) return; // current letter already has a match, stay put
      const matchLetters = new Set(matches.map((e) => letterOf(e.name)));
      const jumpTo = letters.find((l) => matchLetters.has(l));
      if (jumpTo) {
        activeLetter = jumpTo;
        updateNavActiveHighlight();
      }
    }

    function allEntitiesByType() {
      return typeFilter === 'both' ? allEntities : allEntities.filter((e) => e.entity_type === typeFilter);
    }

    function showLetter(letter) {
      activeLetter = letter;
      page = 1;
      updateNavActiveHighlight();
      renderPage();
    }

    function changePage(newPage) {
      if (!Number.isFinite(newPage)) return; // never let a bad value blank the list
      page = Math.max(1, newPage);
      renderPage();
    }

    function buildPager(currentPage, totalPages, total) {
      return window.SRW.buildPager({ page: currentPage, totalPages, total, perPage: PAGE_SIZE, onPage: changePage });
    }

    // Rebuilds just the active letter's paginated content -- called on
    // page change, on search text change, and after renderList()
    // picks/validates the active letter. Search text is applied here
    // (not in renderList()/byLetter) so it narrows the visible list
    // without touching which letters the nav shows.
    function renderPage() {
      contentArea.innerHTML = '';
      if (!activeLetter) {
        contentArea.appendChild(el('p', { className: 'srw-hint', textContent: 'No matches.' }));
        return;
      }
      const q = searchText.trim().toLowerCase();
      const letterEntities = byLetter[activeLetter] || [];
      const list = (q ? letterEntities.filter((e) => (e.name || '').toLowerCase().includes(q)) : letterEntities)
        .slice()
        .sort((a, b) => (a.name || '').localeCompare(b.name || ''));

      contentArea.appendChild(el('h5', {
        className: 'srw-watchlist-letter-heading',
        textContent: `${activeLetter} — ${list.length}`,
      }));
      if (list.length === 0) {
        contentArea.appendChild(el('p', { className: 'srw-hint', textContent: 'No matches on this letter.' }));
        return;
      }
      const totalPages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
      page = Math.min(Math.max(page, 1), totalPages);
      const pageItems = list.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
      const ul = el('ul', { className: 'srw-watchlist srw-watchlist-columns' });
      pageItems.forEach((entity) => ul.appendChild(buildEntityLi(entity)));
      if (totalPages > 1) contentArea.appendChild(buildPager(page, totalPages, list.length)); // top and bottom, like Stash's own lists
      contentArea.appendChild(ul);
      if (totalPages > 1) contentArea.appendChild(buildPager(page, totalPages, list.length));
    }

    function renderList() {
      filterButtons.forEach((btn, i) => btn.classList.toggle('srw-btn-primary', FILTERS[i].key === typeFilter));

      const typeFiltered = typeFilter === 'both'
        ? allEntities
        : allEntities.filter((e) => e.entity_type === typeFilter);

      // Grouped by first letter of the name -- added 2026-09-12 once
      // the watchlist grew past 2000 entries via the StashDB.org/
      // ThePornDB favorites imports, at which point a single flat list
      // became unusable. Non-letter-starting names (rare) bucket under
      // "#", sorted last. Derived from the type filter only, not the
      // text search (2026-09-13) -- see renderPage().
      byLetter = {};
      typeFiltered.forEach((entity) => {
        const first = (entity.name || '').trim().charAt(0).toUpperCase();
        const letter = /[A-Z]/.test(first) ? first : '#';
        (byLetter[letter] = byLetter[letter] || []).push(entity);
      });
      letters = Object.keys(byLetter).sort((a, b) => {
        if (a === '#') return 1;
        if (b === '#') return -1;
        return a.localeCompare(b);
      });
      // Falls back to the first letter that still has matches (rather
      // than showing nothing) when the current one doesn't survive the
      // type filter -- e.g. switching to "Performer" when the active
      // letter had only studios.
      if (!letters.includes(activeLetter)) activeLetter = letters[0] || null;
      page = 1;

      letterNav.innerHTML = '';
      letters.forEach((letter) => letterNav.appendChild(el('button', {
        className: `srw-watchlist-nav-link${letter === activeLetter ? ' srw-watchlist-nav-link-active' : ''}`,
        textContent: letter,
        onClick: () => showLetter(letter),
      })));

      // Re-run the search jump too -- switching the type filter while a
      // search is active (e.g. All -> Performer) can just as easily
      // leave the active letter without a match as typing does.
      applySearchAutoJump();
      renderPage();
    }

    renderList();
  }

  // First-start setup guide (v0.179.0): the same sections as Settings, one step at a time, so there is no second copy of any setting. What is
  // done is read from the real state (GET /setup/status), not from a stored flag. "Later" is remembered by this browser only.
  const SETUP_LATER_KEY = 'srw:setup-later';
  async function renderSetup(container) {
    const { el, notify } = window.SRW;
    container.innerHTML = '';
    container.appendChild(window.SRW.buildPageHeader('settings'));
    // Reads Stash's own API key and stash-boxes into the sidecar first (v0.186.0), so everything below already sees them.
    let stashReport = null;
    try { stashReport = await window.SRW.runOp('stash_sync'); } catch (e) { /* the guide still works; the notes then say Stash could not be read */ }
    const [settings, effective, initial] = await Promise.all([
      window.SRW.runOp('settings_get_all'), window.SRW.runOp('settings_get_effective_connections'), window.SRW.runOp('setup_status'),
    ]);
    const secretPlaceholder = (isSet) => (isSet ? '(active, from .env — enter here to override)' : '(not set)');
    let status = initial && !initial.error ? initial : { steps: [], complete: false, missing: [] };
    let current = 0;
    const stepDone = (key) => Boolean((status.steps.find((x) => x.key === key) || {}).done);

    // Several things can ask for a refresh at about the same time (a field saved, an entry added, the Stash step's own check): only the
    // newest request's answer is applied, so a slower, older answer cannot paint a stale status over it.
    let statusSeq = 0;
    async function refreshStatus() {
      const mine = ++statusSeq;
      const st = await window.SRW.runOp('setup_status');
      if (mine !== statusSeq) return;
      if (st && !st.error) status = st;
      paintNav();
    }
    const onFieldSaved = (key, value) => {
      settings[key] = value;
      if (key.endsWith('_url')) effective[key] = value; else if (key.endsWith('_api_key')) effective[`${key}_set`] = !!value;
      refreshStatus();
    };
    // Asks Stash again (args: {add: [...]} / {overwrite: true}) and brings this guide's copies of the settings and the status up to date.
    async function resync(args = {}) {
      const r = await window.SRW.runOp('stash_sync', args);
      if (r && !r.error) stashReport = r;
      Object.assign(effective, await window.SRW.runOp('settings_get_effective_connections'));
      Object.assign(settings, await window.SRW.runOp('settings_get_all'));
      await refreshStatus();
      return r;
    }
    // What the last sync did to one built-in provider's key, as a line above that provider's own fields.
    function builtinNote(kind, label) {
      const box = ((stashReport && stashReport.boxes) || []).find((b) => b.kind === kind);
      let text;
      if (!stashReport || stashReport.error || stashReport.discovered === false) text = `Stash’s configuration could not be read, so the ${label} key has to be entered by hand.`;
      else if (!box) text = `${label} is not configured in your Stash (Settings > Metadata Providers > Stash-box endpoints). Add it there, or enter the key below.`;
      else if (box.status === 'skipped') text = `Your Stash has the ${label} endpoint but no API key for it. Enter the key below.`;
      else if (box.status === 'filled' || box.status === 'updated') text = `The ${label} API key was taken over from your Stash.`;
      else text = `The ${label} API key is set; your Stash has the endpoint ${box.endpoint}.`;
      return note(text);
    }
    // The fields stay available as an override; they are folded away when the key is already there.
    function overrideBox(label, open, content) {
      const details = el('details', {}, [el('summary', { textContent: label }), content]);
      details.open = open;
      return details;
    }
    const note = (text) => el('p', { className: 'srw-hint', textContent: text });

    const STEPS = [
      { key: 'stash', title: 'Stash', intro: 'The sidecar needs the address under which it reaches your Stash. The API key (and the StashDB / ThePornDB / other '
          + 'stash-box keys) are read from Stash itself, there is nothing to copy.',
        build: async () => {
          const box = el('div');
          const out = el('p', { className: 'srw-hint', textContent: 'checking…' });
          const keyNote = el('p', { className: 'srw-hint' });
          const paintKey = () => {
            const k = stashReport && !stashReport.error ? stashReport.stash_api_key : null;
            if (stashReport && stashReport.discovered === false) keyNote.textContent = 'Stash’s configuration could not be read from here; enter the API key by hand below if your Stash has a login.';
            else if (k === 'updated' || k === 'unchanged') keyNote.textContent = 'Stash API key: taken from your Stash.';
            else if (effective.stash_api_key_set) keyNote.textContent = 'Stash API key: your Stash gave none; the stored one is used.';
            else keyNote.textContent = 'Stash API key: none. Fine while your Stash has no login; with a login, generate an API key in Stash > Settings > Security and press “Check again”.';
          };
          const urlBox = el('div', { className: 'srw-settings-section' });
          const run = async () => {
            out.textContent = 'checking…';
            await resync();
            paintKey();
            const r = await window.SRW.runOp('connections_check', { only: 'stash' });
            const s = (r && r.stash) || {};
            out.textContent = r && r.error ? `Error: ${r.error}` : (s.ok ? 'Stash answers.' : `Stash does not answer: ${s.error || 'no address entered yet'}`);
            await refreshStatus();
          };
          buildConnectionField(urlBox, {
            label: 'Stash URL', key: 'stash_url', value: effective.stash_url || '', placeholder: window.location.origin,
            onSaved: (key, value) => { settings[key] = value; effective[key] = value; run(); },
          });
          const hand = el('div', { className: 'srw-settings-section' });
          buildConnectionField(hand, {
            label: 'Stash API key', key: 'stash_api_key', value: '', isSecret: true,
            placeholder: effective.stash_api_key_set ? '(set)' : '(not set)', onSaved: () => run(),
          });
          box.append(urlBox, keyNote, out,
            el('button', { className: 'srw-btn', textContent: 'Check again', onClick: run }),
            overrideBox('Enter the Stash API key by hand', !!(stashReport && stashReport.discovered === false), hand));
          paintKey();
          run();
          return box;
        } },
      { key: 'stashdb', title: 'StashDB', intro: 'Where new releases of the performers and studios you follow come from. The API key is taken from your Stash when '
          + 'StashDB is set up there; your StashDB account page shows it otherwise.',
        build: async () => {
          const box = el('div');
          box.append(builtinNote('stashdb', 'StashDB'),
            overrideBox('StashDB address and API key', !stepDone('stashdb'), await buildStashDBConnectionSection(settings, effective, secretPlaceholder, onFieldSaved)));
          return box;
        } },
      { key: 'theporndb', title: 'ThePornDB', optional: true, intro: 'Optional: ThePornDB adds more studios and the movie wishlist. Skip it if you do not use it.',
        build: async () => {
          const box = el('div');
          box.append(builtinNote('theporndb', 'ThePornDB'),
            overrideBox('ThePornDB address and API key', !stepDone('theporndb'), await buildThePornDBConnectionSection(settings, effective, secretPlaceholder, onFieldSaved)));
          return box;
        } },
      { key: 'stashboxes', title: 'Other stash-boxes', optional: true,
        intro: 'Optional: further stash-boxes you set up in Stash (JAVStash, FansDB, ...) can be watched for new releases as well.',
        build: async () => buildStashBoxImport({ getReport: () => stashReport, sync: resync, onChange: () => refreshStatus() }) },
      { key: 'search', title: 'Search', intro: 'Where releases are looked for: Prowlarr (one connection, all indexers) or your own Newznab indexers. One of the two is enough.',
        build: async () => {
          const box = el('div');
          box.append(await buildSearchBackendSection(), buildProwlarrConnectionSection(settings, effective, secretPlaceholder, onFieldSaved), await buildIndexersSection(() => refreshStatus()));
          return box;
        } },
      { key: 'download', title: 'Downloads', intro: 'Your download client, and where its finished downloads are for this sidecar: “SABnzbd’s own downloads path” is the '
          + 'folder as SABnzbd calls it, the local mount is the folder you mounted into this container (/downloads in the example).',
        build: async () => {
          const box = el('div');
          box.appendChild(await buildDownloadClientsSection(() => refreshStatus()));
          const paths = wrapSubsection(buildFileImportSection(settings), 'This sidecar’s own downloads mount');
          if (paths) box.appendChild(el('div', { className: 'srw-settings-section' }, [paths]));
          return box;
        } },
      { key: 'quality', title: 'Quality', intro: 'Which resolutions you accept. A release below the lowest one is never downloaded.',
        build: () => buildQualityProfilesSection(settings, () => refreshStatus()) },
      { key: 'content', title: 'Content & watchlist', optional: true, intro: 'What the plugin looks after (scenes, movies, or both) and whom it follows (your Stash favorites, or a tag).',
        build: async () => {
          const box = el('div');
          box.append(buildContentScopeSection(settings), buildWatchlistSourceSection(settings, () => renderSetup(container)));
          return box;
        } },
      { key: 'whisparr', title: 'Whisparr', optional: true, intro: 'Optional: only if you want Whisparr to keep a second record. The program works without it.',
        build: () => buildWhisparrConnectionSection(settings, effective, secretPlaceholder, onFieldSaved) },
      { key: 'userscript', title: 'Browser userscript', optional: true,
        intro: 'Optional: a userscript adds buttons to StashDB\'s pages (scene, scene cards, performer and studio pages) so you can put a scene on the wishlist, search it or watch a performer / studio without leaving the site. The program works without it.',
        build: async () => {
          const { sidecarUrl } = await window.SRW.getSettings();
          const scriptUrl = `${sidecarUrl}/userscript/srm.user.js`;
          const link = el('a', { className: 'srw-btn srw-btn-primary', href: scriptUrl, target: '_blank', rel: 'noopener noreferrer', textContent: 'Install the userscript',
            title: 'Opens the script; with a userscript manager installed it offers to install it' });
          const copy = el('button', { type: 'button', className: 'srw-btn', textContent: 'Copy the address', title: scriptUrl,
            onClick: async () => {
              if (await window.SRW.copyText(scriptUrl)) notify('Userscript', 'Address copied.', 'success');
              else notify('Userscript', 'Copying is blocked here -- select the address below and copy it by hand.', 'info');
            } });
          const steps = el('ol', { className: 'srw-hint' }, [
            'Install a userscript manager in your browser: Tampermonkey or Violentmonkey (a browser extension).',
            'Press "Install the userscript" (or open the address below in that browser). The manager shows the script: confirm the installation and allow the connection to the sidecar when it asks.',
            'Open a StashDB scene: you get the chips "Stash" and "SRM" and the buttons Send to wishlist, Send and search now, Open in Stash and Copy Stash-ID. On a performer or studio page an eye button puts it on the watchlist (the crossed-out eye takes it off again).',
            'Another stash-box from Settings > Connections works after you add an "// @match" line with its address to the script.',
          ].map((t) => el('li', { textContent: t })));
          return el('div', {}, [steps, el('div', { className: 'srw-toolbar' }, [link, copy]),
            el('p', { className: 'srw-hint' }, ['Address: ', el('code', { textContent: scriptUrl, style: 'user-select: all' })]),
            note('The browser must be able to reach this address. The script updates through the manager\'s update check; after an update of this program the sidecar has to be rebuilt for the script to change.')]);
        } },
      { key: 'finish', title: 'Finish', intro: '', build: async () => {
          await refreshStatus();
          const list = el('ul', { className: 'srw-release-list' }, status.steps.map((x) => el('li', { className: 'srw-release-item' }, [
            el('span', { className: 'srw-release-title', textContent: `${x.done ? '✓' : (x.required ? '✗' : '–')} ${x.label}` }),
            el('span', { className: 'srw-hint', textContent: x.detail || '' }),
            x.done ? null : el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Go to this step', onClick: () => show(Math.max(0, STEPS.findIndex((st) => st.key === x.key))) }),
          ])));
          // Not part of the program's own status (a userscript cannot be seen from here): listed as an optional last item with a link to its step.
          list.appendChild(el('li', { className: 'srw-release-item' }, [
            el('span', { className: 'srw-release-title', textContent: '– Browser userscript' }),
            el('span', { className: 'srw-hint', textContent: 'optional: buttons on StashDB\'s pages; how to install it is in this step' }),
            el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Go to this step', onClick: () => show(STEPS.findIndex((st) => st.key === 'userscript')) }),
          ]));
          const go = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Run the first scan now' });
          if (!status.complete) go.disabled = true;
          const goStatus = el('span', { className: 'srw-key-status' });
          go.addEventListener('click', async () => {
            go.disabled = true; goStatus.textContent = ' scanning… this can take a few minutes';
            const r = await window.SRW.runOp('scan_run');
            goStatus.textContent = r && r.error ? ` error: ${r.error}` : ' done';
            if (!(r && r.error)) { try { window.localStorage.removeItem(SETUP_LATER_KEY); } catch (e) { /* ignore */ } window.location.assign('/plugins/stash-release-manager'); }
            go.disabled = false;
          });
          return el('div', {}, [
            note(status.complete ? 'Everything required is entered. The scheduled jobs (scan, search, import) now run by themselves; their intervals are in Settings > Jobs. '
              + 'You can change every entry later under Settings, and open this guide again from there.'
              : `Still missing: ${status.missing.join(', ')}. The program waits quietly until these are entered.`),
            list, el('div', { className: 'srw-toolbar' }, [go, goStatus]),
          ]);
        } },
    ];

    const nav = el('div', { className: 'srw-subtabs srw-section-tabs' });
    const paneHost = el('div', { className: 'srw-settings-section' });
    const footer = el('div', { className: 'srw-toolbar' });
    function paintNav() {
      nav.replaceChildren(...STEPS.map((st, i) => {
        const done = st.key === 'finish' ? status.complete : stepDone(st.key);
        const b = el('button', { type: 'button', className: `srw-btn${i === current ? ' srw-btn-primary' : ''}`,
          textContent: `${done ? '✓ ' : ''}${st.title}${st.optional ? ' (optional)' : ''}` });
        b.addEventListener('click', () => show(i));
        return b;
      }));
    }
    async function show(i) {
      current = Math.max(0, Math.min(STEPS.length - 1, i));
      const st = STEPS[current];
      paintNav();
      const heading = el('h3', { textContent: `${current + 1}. ${st.title}${st.optional ? ' (optional)' : ''}` });
      paneHost.replaceChildren(...[heading, st.intro ? note(st.intro) : null, note('Loading…')].filter(Boolean)); // (replaceChildren turns a null into the text "null")
      const body = await st.build();
      if (STEPS[current] !== st) return; // the user moved on while it was loading
      paneHost.replaceChildren(...[heading, st.intro ? note(st.intro) : null, body].filter(Boolean));
      footer.replaceChildren(
        current > 0 ? el('button', { className: 'srw-btn', textContent: 'Back', onClick: () => show(current - 1) }) : null,
        current < STEPS.length - 1 ? el('button', { className: 'srw-btn srw-btn-primary', textContent: st.optional && !stepDone(st.key) ? 'Skip' : 'Next', onClick: () => show(current + 1) }) : null,
        el('button', { className: 'srw-btn', title: 'Close the guide for now; it stays available under Settings and as a reminder on the Releases page',
          textContent: 'Later', onClick: () => { try { window.localStorage.setItem(SETUP_LATER_KEY, '1'); } catch (e) { /* ignore */ } window.location.assign('/plugins/stash-release-manager'); } }),
      );
    }
    container.append(el('h2', { textContent: 'Setup guide' }),
      note('The entries every new installation needs, once and in one place. Everything you enter here is saved at once and can be changed later under Settings.'),
      nav, paneHost, footer);
    // Start at the first step that still needs something.
    const firstOpen = STEPS.findIndex((st) => st.key !== 'finish' && !st.optional && !stepDone(st.key));
    show(firstOpen >= 0 ? firstOpen : STEPS.length - 1);
  }

  window.SRW_Settings = { render, renderWatchlistPage, renderSetup };
})();
