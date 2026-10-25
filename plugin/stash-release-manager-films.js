/*
 * Film wishlist (v0.108.0) -- the "Films" part of the Releases page. Films/groups the user WANTS, separate from the scene releases
 * (api/film_repo.py, api/film_match.py, api/routers/films.py). Tabs by status; every list is filtered + paged like everywhere else.
 */
(function () {
  'use strict';

  // Same tabs as the scene page: [New releases | On Wishlist | Imported | Dismissed]. "New releases" = the suggestions (list or calendar);
  // Downloading and Import failed live on the Activity page.
  const TABS = [['wanted', 'On Wishlist'], ['upgrade', 'Upgrade'], ['imported', 'Imported'], ['dismissed', 'Dismissed']];
  // 'Upgrade' is a view of imported movies whose owned file is below the quality profile (upgrade_wanted); Imported shows the others
  const viewKey = (f) => (f.status === 'imported' && f.upgrade_wanted ? 'upgrade' : f.status);
  const COUNTED = ['suggested', 'wanted', 'imported', 'dismissed'];
  const CLASS_LABEL = {
    sure: ['sure match', 'Same title, year and length as the ThePornDB movie'],
    likely: ['likely match', 'Same title and year, length differs or is unknown'],
    unsure: ['unsure', 'Only a similar title or another year -- link the right ThePornDB movie yourself'],
    none: ['no match', 'ThePornDB has nothing for this title -- link a movie by hand, search by hand, or dismiss'],
    linked: ['linked by you', 'You picked the ThePornDB movie'],
  };
  const CLASS_ORDER = { sure: 0, likely: 1, linked: 2, unsure: 3, none: 4 };

  // ---------- ThePornDB search panel: "Add film" and "Link movie" ----------
  function buildTpdbSearch({ initial, actionLabel, onPick }) {
    const { el } = window.SRW;
    const input = el('input', { type: 'search', className: 'srw-search-input', value: initial || '' });
    const goBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Go' });
    const st = el('span', { className: 'srw-key-status' });
    const results = el('div');
    async function run() {
      const q = input.value.trim();
      if (!q) return;
      window.SRW.setBusy(st, 'searching...');
      results.innerHTML = '';
      const res = await window.SRW.runOp('films_search_tpdb', { q });
      if (res.error) { st.textContent = ` error: ${res.error}`; return; }
      st.textContent = '';
      const rows = res.results || [];
      if (!rows.length) { results.appendChild(el('p', { className: 'srw-hint', textContent: 'No movies found.' })); return; }
      rows.forEach((m) => {
        const meta = [m.year, m.studio_name, m.duration_min ? `${Math.round(m.duration_min)} min` : null,
          (m.performers || []).length ? `${m.performers.length} performers` : null].filter(Boolean).join(' — ');
        results.appendChild(el('div', { className: 'srw-manual-search-result' }, [
          m.image_url ? el('img', { className: 'srw-film-thumb srw-film-thumb-sm', src: m.image_url, loading: 'lazy', alt: '' }) : null,
          el('span', { className: 'srw-release-title', textContent: m.title }),
          el('span', { className: 'srw-review-candidate-meta', textContent: meta }),
          m.on_list ? el('span', { className: 'srw-unverified-badge', textContent: 'already on the list' })
            : el('button', { className: 'srw-btn srw-btn-primary', textContent: actionLabel, onClick: () => onPick(m) }),
        ]));
      });
    }
    goBtn.addEventListener('click', run);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') run(); });
    return { node: el('div', { className: 'srw-manual-search-panel' }, [el('span', {}, [input, goBtn, st]), results]), run };
  }

  function buildPane() {
    const { el, notify, confirmInline } = window.SRW;
    const root = el('div', { className: 'srw-films' });
    let films = [];
    const counts = el('span', { hidden: true }); // text changes -> the tab labels follow (buildPaneTabs `watch`)
    let sidecarUrl = ''; // for the saved seed covers (<img> may load them across origins)
    const views = {};
    const panes = {};
    const status = el('span', { className: 'srw-key-status' });

    const countOf = (s) => films.filter((f) => viewKey(f) === s).length;

    // The two suggestion buttons only show while there is something to decide.
    function syncSuggestions() {
      const n = countOf('suggested');
      if (acceptAllBtn) acceptAllBtn.hidden = n === 0;
      if (rematchBtn) rematchBtn.hidden = n === 0;
      suggestBar.hidden = n === 0 && !status.textContent;
    }

    async function load() {
      const res = await window.SRW.runOp('films_list', { per_page: 0, order: 'title' });
      if (res.error) { status.textContent = ` error: ${res.error}`; return; }
      films = res.rows || [];
      counts.textContent = COUNTED.map((k) => countOf(k)).join('/');
      Object.values(views).forEach((v) => v.reload());
      syncSuggestions();
      if (!calPane.hidden) renderCalendar();
    }

    // Whisparr bookkeeping of a movie (v0.166.0), read from the stored states (no request): whisparr_state = the movie entry, whisparr_file_state =
    // whether Whisparr took the library file over. Nothing is shown while no state is recorded (Whisparr off / not yet synced). The provider name
    // is kept in this one place (see the multi-backend direction).
    const BACKEND_NAME = 'Whisparr';
    const BOOKKEEPING_LABEL = { queued: 'waiting to be registered', registered: 'registered', linked: 'linked to its entry', adopted: 'took over an existing entry',
      conflict: 'conflict with another entry', not_found: 'not found', error: 'last attempt failed' };
    const FILE_LABEL = { queued: 'file not handed over yet', synced: 'holds the library file', not_taken: 'did not take the file', conflict: 'file conflict', error: 'file hand-over failed' };
    const BOOKKEEPING_WARN = new Set(['conflict', 'not_found', 'error']);
    const FILE_WARN = new Set(['not_taken', 'conflict', 'error']);
    function whisparrBadge(f) {
      if (!f.whisparr_state && !f.whisparr_file_state) return null;
      const parts = [];
      if (f.whisparr_state) parts.push(BOOKKEEPING_LABEL[f.whisparr_state] || f.whisparr_state);
      if (f.whisparr_file_state) parts.push(FILE_LABEL[f.whisparr_file_state] || f.whisparr_file_state);
      const warn = BOOKKEEPING_WARN.has(f.whisparr_state) || FILE_WARN.has(f.whisparr_file_state);
      return el('span', { className: `srw-release-whisparr-file srw-film-whisparr${warn ? ' srw-film-whisparr-warn' : ''}`,
        textContent: `${BACKEND_NAME}: ${parts.join(', ')}`, title: f.whisparr_note || undefined });
    }

    // ---------- one film row ----------
    function buildRow(f) {
      const [clsLabel, clsTitle] = CLASS_LABEL[f.match_class] || [null, null];
      const meta = [f.year, f.studio_name, f.duration_min ? `${Math.round(f.duration_min)} min` : null,
        (f.performers || []).length ? `${f.performers.length} performers` : null].filter(Boolean).join(' — ');
      let li = null;
      const act = [];
      // The seed = the deleted (or still empty) Stash group this movie came from. A deleted group has no page any more: its saved cover and
      // its original shop page (AdultDVDEmpire, ...) open instead; a group that still exists opens in Stash.
      const seedGroup = f.source === 'empty-group' && f.stash_group_id;
      const seedHref = seedGroup ? `/groups/${f.stash_group_id}` : ((f.seed && f.seed.urls && f.seed.urls[0]) || null);
      const seedCoverSrc = f.source === 'deleted-group' ? `${sidecarUrl}/films/${f.id}/seed-cover` : (seedGroup ? `/group/${f.stash_group_id}/frontimage` : null);
      const seedTitle = seedGroup ? 'Open the group in Stash (new tab)' : 'Open the original page of this seed (new tab) -- the group itself was deleted from Stash';
      const mkLink = (href, child, title) => (href ? el('a', { href, target: '_blank', rel: 'noopener noreferrer', title }, [child]) : child);
      const seedLine = (text) => {
        let cover = null;
        if (seedCoverSrc && sidecarUrl !== null) {
          cover = el('img', { className: 'srw-film-thumb srw-film-thumb-sm', src: seedCoverSrc, loading: 'lazy', alt: '' });
          cover.addEventListener('error', () => cover.remove());
        }
        return el('div', { className: 'srw-hint srw-seed-line' }, [cover ? mkLink(seedHref, cover, seedTitle) : null, mkLink(seedHref, el('span', { textContent: text }), seedTitle)]);
      };
      const setStatus = async (to, label) => {
        const res = await window.SRW.runOp('films_set_status', { id: f.id, status: to });
        if (res.error) { notify('Movie', res.error, 'error'); return; }
        if (to === 'dismissed') {
          window.SRW.dismissedNotice(`Dismissed: ${f.title}`, '',
            () => window.SRW.runOp('films_set_status', { id: f.id, status: res.previous }), () => load());
        } else if (label) {
          notify('Movie', label, 'success');
        }
        await load();
      };
      if (f.status === 'suggested') {
        act.push(el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Accept',
          title: 'Put this movie on the wishlist', onClick: () => setStatus('wanted', `"${f.title}" is on the wishlist.`) }));
      }
      // Own query + Grab, the same panel as the scene wishlist (v0.109.0). The prefill is "Title Year" (film release names carry
      // title and year, not studio and date).
      let manualSlot = null; // joined with "Search now" below, like the buttons of a scene row (v0.243.4)
      if (['wanted', 'import_failed'].includes(f.status) || viewKey(f) === 'upgrade') {
        const searchSlot = el('span', { className: 'srw-search-actions' });
        manualSlot = searchSlot;
        window.SRW.getSearchCapabilities().then((caps) => {
          const sources = (caps && caps.manual_sources) || [];
          if (!sources.length) return;
          const asRelease = { provider: 'theporndb', scene_stashdb_id: f.tpdb_id || `film-${f.id}`, title: `${f.title}${f.year ? ` ${f.year}` : ''}` };
          searchSlot.appendChild(window.SRW.buildManualSearch(asRelease, sources, caps.backend, () => load(), {
            host: () => li,
            grab: (candidate) => window.SRW.runOp('films_grab', { id: f.id, candidate }),
            // Stuck: the search finds one scene of the movie -> it joins the movie's group, the movie stays wanted for the other parts (v0.222.1)
            grabAsScene: f.status === 'wanted' ? (candidate) => window.SRW.runOp('films_grab', { id: f.id, candidate, as_scene: true }) : null,
          }));
        });
      }
      if (f.status === 'wanted' && f.parts_imported) { // scenes were added with "Accept as one scene" (v0.221.0): done when all parts are in
        act.push(el('span', { className: 'srw-hint', textContent: `${f.parts_imported} part(s) in the group` }));
        act.push(el('button', { className: 'srw-btn', textContent: 'All parts are in',
          title: 'Take the movie off the wishlist: the Group built from the parts is the finished movie',
          onClick: async (e) => {
            e.currentTarget.disabled = true;
            const res = await window.SRW.runOp('films_parts_done', { id: f.id });
            if (res.error) notify('Movie', res.error, 'error'); else notify('Movie', `"${f.title}" is complete.`, 'success');
            await load();
          } }));
      }
      if (f.status === 'wanted' || viewKey(f) === 'upgrade') { // the automatic search for this one movie, now (v0.128.0; an owned movie: a better version, v0.222.0): the result is a candidate in Activity > Needs review
        const searchNowBtn = window.SRW.iconButton('button', 'faMagnifyingGlass', 'Search now', {
          title: viewKey(f) === 'upgrade' ? 'Search now: ask the indexers for a BETTER version of this movie (1-3 requests); a candidate waits in Activity > Needs review'
            : 'Search now: ask the indexers for this movie (1-3 requests); a plausible release becomes a candidate in Activity > Needs review',
          onClick: async (e) => {
            e.currentTarget.disabled = true;
            window.SRW.busyIcon(e.currentTarget);
            const res = await window.SRW.runOp('films_search', { id: f.id });
            if (res.error) notify('Movie search', res.error, 'error');
            else if (res.result === 'needs_review') notify('Movie search', `Candidate found: ${res.candidate.title} -- see Activity > Needs review.`, 'success');
            else notify('Movie search', res.result === 'no_match' ? `Nothing ${viewKey(f) === 'upgrade' ? 'better' : 'usable'} found (${(res.queries || []).join(' | ')}).`
              : (res.result === 'not_searched' ? res.reason : `Search failed: ${res.error || ''}`), 'info');
            await load();
          } });
        act.push(el('span', { className: 'srw-btn-group' }, [searchNowBtn, manualSlot].filter(Boolean)));
        manualSlot = null;
      }
      if (manualSlot) act.push(manualSlot); // no "Search now" (import failed): Manual alone
      // Phase 1b (v0.111.0): is the film on StashDB as SCENES ("<Series> N - Scene K")? One StashDB request per check, only on click;
      // the last result is kept on the film. At >= 80 % of the film's length the scenes can replace the one-file search.
      if (['suggested', 'wanted'].includes(f.status)) {
        const showCheck = (c) => {
          const old = li.querySelector('.srw-stashdb-check');
          if (old) old.remove();
          const lines = (c.scenes || []).map((x) => el('div', { className: 'srw-manual-search-result' }, [
            el('span', { className: 'srw-release-title', textContent: `Scene ${x.k}` }),
            el('span', { textContent: x.title }),
            el('span', { className: 'srw-review-candidate-meta',
              textContent: [x.studio, x.date, x.minutes ? `${Math.round(x.minutes)} min` : null, (x.performers || []).slice(0, 3).join(', '),
                x.duplicates ? `+${x.duplicates} duplicate` : null].filter(Boolean).join(' — ') }),
          ]));
          const others = Object.entries(c.other_films || {}).map(([n, ks]) => `${n} (scene ${ks.join(', ')})`);
          const panel = el('div', { className: 'srw-manual-search-panel srw-stashdb-check' }, [
            el('p', { className: c.offer ? 'srw-search-status' : 'srw-hint', textContent: c.message }),
            ...lines,
            others.length && !(c.scenes || []).length ? el('p', { className: 'srw-hint', textContent: `Other movies of this series on StashDB: ${others.join('; ')}` }) : null,
            c.incomplete ? el('p', { className: 'srw-hint', textContent: 'StashDB has more hits than were read -- the list may be incomplete.' }) : null,
            el('div', {}, [
              c.offer ? el('button', { className: 'srw-btn srw-btn-primary', textContent: `Use these ${c.scenes.length} scenes`,
                title: 'Put the scenes on the scene wishlist; when all are imported the Group is built',
                onClick: (e) => confirmInline(e.currentTarget,
                  `Put the ${c.scenes.length} StashDB scenes of "${f.title}" on the scene wishlist and build the Group from them once they are imported?`,
                  async () => {
                    const res = await window.SRW.runOp('films_use_scenes', { id: f.id });
                    if (res.error) { notify('Use scenes', res.error, 'error'); return; }
                    notify('Use scenes', `${res.scenes} scene(s) are on the scene wishlist (${res.new} new). The Group follows when all are imported.`, 'success');
                    await load();
                  }) }) : null,
              window.SRW.iconButton('button', 'faClipboardCheck', 'Check again', { className: 'srw-btn', title: 'Check again: one StashDB request',
                onClick: () => runCheck() }),
              el('button', { className: 'srw-btn', textContent: 'Close', onClick: () => panel.remove() }),
            ]),
          ]);
          li.appendChild(panel);
        };
        const runCheck = async () => {
          const res = await window.SRW.runOp('films_check_stashdb', { id: f.id });
          if (res.error) { notify('StashDB check', res.error, 'error'); return; }
          f.stashdb = res;
          showCheck(res);
        };
        act.push(window.SRW.iconButton('button', ['faMagnifyingGlass', 'faList'], 'Check StashDB scenes', { className: 'srw-btn',
          title: f.stashdb ? 'Show the last result (no request)' : 'Looks for this movie as scenes on StashDB (one request)',
          onClick: () => { if (li.querySelector('.srw-stashdb-check')) { li.querySelector('.srw-stashdb-check').remove(); return; }
            if (f.stashdb) showCheck(f.stashdb); else runCheck(); } }));
      }
      if (['suggested', 'wanted'].includes(f.status) && (!f.tpdb_id || f.match_class === 'unsure')) {
        act.push(window.SRW.iconButton('button', 'faLink', 'Link movie…', { className: 'srw-btn',
          title: 'Pick the ThePornDB movie that belongs to this entry',
          onClick: () => {
            const old = li.querySelector('.srw-manual-search-panel');
            if (old) { old.remove(); return; }
            const panel = buildTpdbSearch({
              initial: f.title, actionLabel: 'Use this movie',
              onPick: async (m) => {
                const res = await window.SRW.runOp('films_link', { id: f.id, movie: m.record });
                if (res.error) { notify('Link movie', res.error, 'error'); return; }
                notify('Link movie', `"${f.title}" is linked to "${m.title}".`, 'success');
                await load();
              },
            });
            li.appendChild(panel.node);
            panel.run();
          } }));
      }
      // Dismiss, and "Dismiss & delete group" for a movie of an empty Stash group (shared with Activity > Stuck, v0.214.1).
      act.push(...window.SRW.buildFilmDismissButtons(f, () => load()));
      // Import (v0.110.0): files already in the library but not grouped -- retry, or build the Group although the length did not fit.
      if (['grabbed', 'import_failed'].includes(f.status) && f.pending_import_path) {
        const force = f.status === 'import_failed';
        act.push(el('button', { className: force ? 'srw-btn' : 'srw-btn', textContent: force ? 'Group anyway' : 'Retry grouping',
          title: force ? 'The files are in the library but their length does not match the ThePornDB movie: build the Group regardless'
            : 'Check again whether Stash has scanned the file(s), then build the Group',
          onClick: (e) => confirmInline(e.currentTarget,
            force ? `Build the Group for "${f.title}" although the length does not match?` : `Try to build the Group for "${f.title}" now?`,
            async () => {
              const res = await window.SRW.runOp('films_finalize', { id: f.id, force });
              if (res.error) { notify('Group', res.error, 'error'); return; }
              notify('Group', res.status === 'imported' ? `"${f.title}" is imported (group ${res.group_id}).`
                : (res.status === 'waiting' ? 'Stash has not scanned every file yet -- try again in a moment.' : (f.import_error || res.status)),
              res.status === 'imported' ? 'success' : 'info');
              await load();
            }) }));
      }
      if (viewKey(f) === 'upgrade') {
        if (f.imported_group_id) {
          act.push(window.SRW.iconButton('a', 'faPlay', 'Open group', { href: `/groups/${f.imported_group_id}`, target: '_blank', rel: 'noopener noreferrer',
            title: 'Open group: the group in Stash (new tab)' }));
        }
        act.push(el('button', { className: 'srw-btn', textContent: 'Keep this file',
          title: 'The file stays as it is; the movie leaves the Upgrade list',
          onClick: async (e) => {
            e.currentTarget.disabled = true;
            const res = await window.SRW.runOp('films_upgrade_keep', { id: f.id });
            if (res.error) notify('Movie', res.error, 'error'); else notify('Movie', `"${f.title}": the file stays.`, 'success');
            await load();
          } }));
      }
      if (f.status === 'dismissed') {
        act.push(window.SRW.iconButton('button', 'faRotateLeft', 'Restore', { className: 'srw-btn', title: 'Restore: back on the wishlist', onClick: () => setStatus('wanted', `"${f.title}" is back on the wishlist.`) }));
      }
      li = el('li', { className: f.tpdb_id ? 'srw-release-item srw-release-item-theporndb srw-film-row' : 'srw-release-item srw-film-row' }, [
        f.image_url ? mkLink(f.tpdb_url, el('img', { className: 'srw-film-thumb', src: f.image_url, loading: 'lazy', alt: '' }), 'Open this movie on ThePornDB (new tab)')
          : el('span', { className: 'srw-film-thumb srw-film-thumb-empty', textContent: '🎬' }),
        el('div', { className: 'srw-row-info' }, [
          el('div', {}, [el('span', { className: 'srw-release-title', textContent: f.title }),
            clsLabel ? el('span', { className: `srw-film-class srw-film-class-${f.match_class}`, textContent: clsLabel, title: clsTitle }) : null]),
          whisparrBadge(f),
          el('div', { className: 'srw-hint' }, [
            el('span', { textContent: meta || 'no details yet' }),
            f.tpdb_url ? el('a', { href: f.tpdb_url, target: '_blank', rel: 'noopener noreferrer', textContent: '  ThePornDB ↗', title: 'Open this movie on ThePornDB' }) : null]),
          // What we knew before the match, next to what ThePornDB says (v0.116.0); also without a ThePornDB id (v0.167.1), so a movie that still
          // needs "Link movie…" shows which Stash group / shop page it came from
          f.seed ? seedLine(`Seed: ${[f.seed.title, f.seed.year, f.seed.studio,
            f.seed.minutes ? `${Math.round(f.seed.minutes)} min` : null].filter(Boolean).join(' — ')}${seedHref ? ' ↗' : ''}`) : null,
          f.note ? el('div', { className: 'srw-hint', textContent: f.note }) : null,
          // The best candidates of the last match with one-click "Use" (no second search)
          f.status === 'suggested' && !f.tpdb_id && (f.candidates || []).length ? el('div', {}, [
            f.seed ? seedLine(`Seed: ${[f.seed.title, f.seed.year, f.seed.studio, f.seed.minutes ? `${Math.round(f.seed.minutes)} min` : null].filter(Boolean).join(' — ')}${seedHref ? ' ↗' : ''} -- candidates from ThePornDB:`)
              : el('div', { className: 'srw-hint', textContent: 'Candidates from ThePornDB:' }),
            ...f.candidates.slice(0, 3).map((c) => el('div', { className: 'srw-manual-search-result' }, [
              el('span', { className: 'srw-release-title', textContent: `${c.title}${c.year ? ` (${c.year})` : ''}` }),
              el('span', { className: 'srw-review-candidate-meta', textContent: [c.site, c.minutes ? `${Math.round(c.minutes)} min` : null, c.performers ? `${c.performers} performers` : null,
                c.length_ok ? 'length fits' : null].filter(Boolean).join(' — ') }),
              c.url ? el('a', { href: c.url, target: '_blank', rel: 'noopener noreferrer', textContent: 'ThePornDB ↗' }) : null,
              el('button', { className: 'srw-btn', textContent: 'Use', title: 'This is the movie',
                onClick: async () => {
                  const res = await window.SRW.runOp('films_use_candidate', { id: f.id, tpdb_id: c.id });
                  if (res.error) { notify('Use candidate', res.error, 'error'); return; }
                  notify('Use candidate', `"${f.title}" is linked to "${c.title}".`, 'success');
                  await load();
                } }),
            ])),
          ]) : null,
          viewKey(f) === 'upgrade' ? el('div', { className: 'srw-hint', textContent: `The file in the group is ${f.owned_height || '?'}p, below the quality profile -- a better version is searched${f.review_candidate ? ' (a candidate waits in Activity > Needs review)' : (f.last_search_result === 'no_match' ? ' (nothing better found at the last search)' : (f.last_search_result === 'upgrade_manual' ? ' -- not searched: the group holds several scenes or files, replace the file by hand' : ''))}` }) : null,
          f.import_error ? el('div', { className: 'srw-search-status', textContent: f.import_error }) : null,
          f.grabbed_title ? el('div', { className: 'srw-hint', textContent: `Download: ${f.grabbed_title}` }) : null,
          // Whisparr bookkeeping (like the scenes: Whisparr is told about the movie per the Whisparr scope preset)
          f.whisparr_id ? el('div', { className: 'srw-hint', textContent: 'In Whisparr' }) : null,
          f.status === 'grabbed' && f.mode === 'scenes' ? el('div', { className: 'srw-search-status',
            textContent: `Put together from StashDB scenes: ${f.scenes_imported || 0} of ${f.scenes_total || (f.scene_ids || []).length} imported -- the scenes are on Scenes > On Wishlist, the Group follows when all are in` }) : null,
          f.status === 'grabbed' && f.mode !== 'scenes' ? el('div', { className: 'srw-search-status', textContent: f.pending_import_path
            ? 'In the library -- waiting for Stash to scan the file(s), then the Group is built'
            : 'Downloading / waiting for the import' }) : null,
        ]),
        el('div', { className: 'srw-row-actions' }, act),
      ]);
      return li;
    }

    // ---------- tabs ----------
    // The filter row is the scene page's own (SRW_Releases.buildFilterBar): presets, search (+ description / tags), All | Studios | Performer,
    // All | VR | Non-VR, StashDB | ThePornDB, Show imported. For movies: "Studios" = movies that came in through a watched studio (the feed),
    // "Performer" = through a watched performer (none yet), VR = a studio with "VR" in its name, every movie is a ThePornDB movie.
    const newFilterState = (importedKey) => {
      let showImported = false;
      try { showImported = importedKey ? localStorage.getItem(importedKey) === '1' : false; } catch (e) { /* ignore */ }
      return { search: '', description: false, tags: false, type: 'both', vr: 'all', providers: { stashdb: true, theporndb: true }, showImported };
    };
    const isVrMovie = (f) => /\bvr\b/i.test(f.studio_name || '');
    function passesFilter(f, S) {
      const q = S.search.trim().toLowerCase();
      if (q) {
        let hay = `${f.title} ${f.studio_name || ''}`;
        if (S.description) hay += ` ${f.description || ''}`;
        if (S.tags) hay += ` ${(f.tags || []).join(' ')}`;
        if (!hay.toLowerCase().includes(q)) return false;
      }
      if (S.type === 'studio' && f.source !== 'watched-studio') return false;
      if (S.type === 'performer') return false;
      if (S.vr === 'vr' && !isVrMovie(f)) return false;
      if (S.vr === 'non_vr' && isVrMovie(f)) return false;
      return S.providers.theporndb; // every movie is a ThePornDB movie
    }
    const filterIsActive = (S) => Boolean(S.search.trim() || S.type !== 'both' || S.vr !== 'all' || !S.providers.stashdb || !S.providers.theporndb);
    const BAR_TYPE_TITLE = 'Studios: movies that came in through a watched studio. Performer: through a watched performer (none yet).';
    const classRank = (f) => (CLASS_ORDER[f.match_class] ?? 5);
    // One list tab: the scene page's filter bar above a paged list of the movies in that state.
    const makeList = (key, withPresets) => {
      const S = newFilterState(null);
      const view = window.SRW.buildFilteredPagedList({
        filters: [], perPage: window.SRW.listViewPageSize || 100,
        emptyText: key === 'suggested' ? 'No new movies to decide on. New movies of your watched studios arrive here.'
          : (key === 'upgrade' ? 'No movie file below your quality profile.' : 'Nothing here.'),
        filteredEmptyText: 'Nothing matches the current filter.',
        fetchPage: window.SRW.localPageFetcher(
          () => films.filter((f) => viewKey(f) === key && passesFilter(f, S)).sort((a, b) => (key === 'suggested' ? classRank(a) - classRank(b) : 0)
            || (a.title || '').localeCompare(b.title || '')),
          {}),
        renderItems: (items) => el('ul', { className: 'srw-release-list' }, items.map(buildRow)),
      });
      const barSlot = el('div');
      const buildBar = () => barSlot.replaceChildren(window.SRW_Releases.buildFilterBar(S, () => view.reload(),
        { noun: 'movie', noPresets: !withPresets, noImported: true, typeTitle: BAR_TYPE_TITLE, providerKeys: ['theporndb'] }));
      return { view, buildBar, node: el('div', {}, [barSlot, view.node]) };
    };
    const barBuilders = []; // built once the saved presets are loaded (the preset dropdown reads them when it is built)
    TABS.forEach(([key]) => {
      const l = makeList(key, false);
      views[key] = l.view; barBuilders.push(l.buildBar);
      panes[key] = el('div', { hidden: true }, [l.node]);
    });
    const suggestedList = makeList('suggested', true);
    views.suggested = suggestedList.view; barBuilders.push(suggestedList.buildBar);
    // ---------- existing Stash groups with more than 3 files (v0.112.0) ----------
    let groups = null; // null = not loaded yet (loaded when the tab is opened)
    const checkCache = {}; // group id -> the StashDB check (one request per group and page load)
    const groupsPane = el('div', { hidden: true });
    const groupsStatus = el('p', { className: 'srw-hint', textContent: 'Loading…' });
    function buildGroupRow(g) {
      let li = null;
      const closePanels = () => li.querySelectorAll('.srw-group-panel').forEach((n) => n.remove());
      const checkBtn = window.SRW.iconButton('button', ['faMagnifyingGlass', 'faList'], 'Check StashDB scenes', { className: 'srw-btn',
        title: 'Is this movie on StashDB as scenes? One StashDB request; nothing is changed',
        onClick: async () => {
          closePanels();
          const res = await window.SRW.runOp('films_group_check', { group_id: g.id });
          if (res.error) { notify('StashDB check', res.error, 'error'); return; }
          const lines = (res.scenes || []).map((x) => el('div', { className: 'srw-manual-search-result' }, [
            el('span', { className: 'srw-release-title', textContent: `Scene ${x.k}` }), el('span', { textContent: x.title }),
            el('span', { className: 'srw-review-candidate-meta', textContent: [x.studio, x.date, x.minutes ? `${Math.round(x.minutes)} min` : null].filter(Boolean).join(' — ') })]));
          const pairs = (res.pairs || []).map((x) => el('div', { className: 'srw-hint',
            textContent: `${x.file} (${x.local_minutes} min) looks like StashDB "${x.stashdb_title}" (${x.stashdb_minutes} min)` }));
          const panel = el('div', { className: 'srw-manual-search-panel srw-group-panel' }, [
            el('p', { className: res.offer ? 'srw-search-status' : 'srw-hint', textContent: res.message }), ...lines,
            pairs.length ? el('p', { className: 'srw-hint', textContent: 'Possible matches by length (display only, nothing is linked):' }) : null, ...pairs,
            el('div', {}, [el('button', { className: 'srw-btn', textContent: 'Close', onClick: () => panel.remove() })])]);
          li.appendChild(panel);
        } });
      // Assign the scenes by hand (v0.114.0): per local scene look at it (thumbnail + link to Stash), then pick one of the film's known StashDB
      // scenes, or -- if none is known -- give it a name yourself. The one StashDB request is made when the panel opens (cached per page).
      const scenesBtn = window.SRW.iconButton('button', 'faList', 'Scenes…', { className: 'srw-btn srw-btn-primary',
        title: 'Look at each scene and assign it to a known StashDB scene of this movie, or name it yourself',
        onClick: async () => {
          if (li.querySelector('.srw-group-panel[data-kind="assign"]')) { closePanels(); return; }
          closePanels();
          const loading = el('div', { className: 'srw-manual-search-panel srw-group-panel', 'data-kind': 'assign' }, [
            el('p', { className: 'srw-hint', textContent: 'Looking the movie up on StashDB (one request)…' })]);
          li.appendChild(loading);
          let check = checkCache[g.id];
          if (!check) {
            check = await window.SRW.runOp('films_group_check', { group_id: g.id });
            if (!check.error) checkCache[g.id] = check;
          }
          const preview = await window.SRW.runOp('films_group_name_preview', { group_id: g.id });
          const derived = {};
          ((preview && preview.items) || []).forEach((x) => { derived[x.scene_id] = x.title; });
          loading.remove();
          const known = check.error ? [] : (check.scenes || []);
          const suggested = {};
          (check.pairs || []).forEach((x) => { suggested[x.scene_id] = x.stashdb_id; });
          const rows = g.scene_list.map((sc) => {
            const select = el('select', { className: 'srw-filter-preset-select' }, [
              el('option', { value: '', textContent: 'keep as it is' }),
              ...known.map((k) => el('option', { value: `s:${k.id}`, textContent: `Scene ${k.k}: ${k.title} (${Math.round(k.minutes)} min)` })),
              el('option', { value: 'm', textContent: 'my own name…' }),
            ]);
            const input = el('input', { type: 'text', className: 'srw-search-input', value: derived[sc.id] || sc.title || '', hidden: true });
            const sync = () => { input.hidden = select.value !== 'm'; };
            select.addEventListener('change', sync);
            if (!sc.title && !sc.has_id) select.value = suggested[sc.id] ? `s:${suggested[sc.id]}` : (known.length ? '' : 'm');
            sync();
            return { sc, select, input, node: el('div', { className: 'srw-manual-search-result srw-group-scene' }, [
              el('a', { className: 'srw-vr-thumb-link', href: `/scenes/${sc.id}`, target: '_blank', rel: 'noopener noreferrer', title: 'Open the scene in Stash (new tab) to watch it' },
                [el('img', { className: 'srw-vr-thumb', src: `/scene/${sc.id}/screenshot`, loading: 'lazy', alt: '' })]),
              el('div', {}, [
                el('a', { className: 'srw-release-title', href: `/scenes/${sc.id}`, target: '_blank', rel: 'noopener noreferrer',
                  textContent: sc.title || '(no title)' }),
                el('div', { className: 'srw-hint', textContent: `${sc.file} — ${Math.round(sc.minutes)} min${sc.has_id ? ' — has a provider id' : ''}${suggested[sc.id] ? ' — length fits a known scene' : ''}` }),
              ]),
              select, input]) };
          });
          const note = check.error ? `StashDB check failed: ${check.error} -- you can still name the scenes.`
            : (known.length ? `${known.length} known StashDB scene(s) of this movie. ${check.message}` : `${check.message} Name the scenes yourself.`);
          const panel = el('div', { className: 'srw-manual-search-panel srw-group-panel', 'data-kind': 'assign' }, [
            el('p', { className: 'srw-hint', textContent: note }),
            el('p', { className: 'srw-hint', textContent: 'Picking a StashDB scene applies its metadata to the local scene like an import (title, date, studio, performers, StashDB id) and its "Scene k" becomes the Scene Number in the group; '
              + '“my own name” only sets the title. A backup of the scenes is written first; Undo restores them.' }),
            ...rows.map((r) => r.node),
            el('div', {}, [
              el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Apply',
                onClick: (e) => {
                  const items = rows.map((r) => {
                    if (r.select.value.startsWith('s:')) { // a known StashDB scene: its "Scene k" is the Scene Number in the group (v0.127.2)
                      const k = known.find((x) => `s:${x.id}` === r.select.value);
                      return { scene_id: r.sc.id, stashdb_id: r.select.value.slice(2), ...(k && Number.isFinite(Number(k.k)) ? { scene_index: Number(k.k) } : {}) };
                    }
                    if (r.select.value === 'm' && r.input.value.trim()) return { scene_id: r.sc.id, title: r.input.value.trim() };
                    return null;
                  }).filter(Boolean);
                  if (!items.length) { notify('Scenes', 'Nothing chosen.', 'info'); return; }
                  confirmInline(e.currentTarget, `Apply ${items.length} change${items.length === 1 ? '' : 's'} to the scenes in Stash? A backup is written first.`, async () => {
                    const done = await window.SRW.runOp('films_group_assign', { group_id: g.id, items });
                    if (done.error) { notify('Scenes', done.error, 'error'); return; }
                    if (done.errors && done.errors.length) notify('Scenes', `${done.errors.length} failed: ${done.errors.map((x) => `scene ${x.scene_id}: ${x.error}`).join('; ')}`, 'error');
                    if (done.applied.length) window.SRW.dismissedNotice(`${done.applied.length} scene(s) assigned`, `Backup: ${done.backup}`,
                      () => window.SRW.runOp('films_group_assign_undo', { applied: done.applied }), () => loadGroups());
                    await loadGroups();
                  });
                } }),
              el('button', { className: 'srw-btn', textContent: 'Close', onClick: () => panel.remove() })]),
          ]);
          li.appendChild(panel);
        } });
      li = el('li', { className: 'srw-release-item srw-film-row' }, [
        el('div', { className: 'srw-row-info' }, [
          el('div', {}, [el('span', { className: 'srw-release-title', textContent: g.name }),
            g.untitled ? el('span', { className: 'srw-film-class srw-film-class-unsure', textContent: `${g.untitled} untitled` }) : null,
            g.with_provider_id ? el('span', { className: 'srw-film-class srw-film-class-sure', textContent: `${g.with_provider_id} with provider id` }) : null]),
          el('div', { className: 'srw-hint', textContent: [g.studio, g.date, `${g.files} files in ${g.scenes} scenes`,
            `${Math.round(g.file_minutes)} min${g.group_minutes ? ` (group says ${Math.round(g.group_minutes)})` : ''}`].filter(Boolean).join(' — ') }),
          el('div', { className: 'srw-hint', textContent: g.scene_list.map((x) => `${x.title || '(no title)'} · ${x.file}`).slice(0, 8).join('  |  ') }),
        ]),
        el('div', { className: 'srw-row-actions' }, [scenesBtn, checkBtn]),
      ]);
      return li;
    }
    const groupsView = window.SRW.buildFilteredPagedList({
      filters: ['search'], perPage: 40, showFilterBarFrom: 15, emptyText: 'No group with more than 3 files.',
      fetchPage: window.SRW.localPageFetcher(() => groups || [], { text: (g) => `${g.name} ${g.studio || ''}` }),
      renderItems: (items) => el('ul', { className: 'srw-release-list' }, items.map(buildGroupRow)),
    });
    groupsPane.append(el('p', { className: 'srw-hint', textContent: 'Groups already in Stash with MORE THAN 3 files -- normally a movie that is split into scenes. '
      + 'Check whether StashDB has the movie as scenes, or give scenes without a title a name derived from their file name.' }), groupsStatus, groupsView.node);
    async function loadGroups() {
      const res = await window.SRW.runOp('films_groups');
      if (res.error) { groupsStatus.textContent = `error: ${res.error}`; return; }
      groups = res.groups || [];
      groupsStatus.textContent = '';
      counts.textContent += ' '; // refresh the tab label
      groupsView.reload();
    }
    // ---------- New releases: [List | Calendar] like the scene page ----------
    // Every movie on the lists with a release date -- also future ones (ThePornDB announces movies ahead of time) -- in the scene calendar's grid,
    // pills, colours (a new movie of ThePornDB = amber like a ThePornDB scene), icons and legend.
    const calState = newFilterState('srw:film-cal-imported');
    const calPane = el('div', { hidden: true });
    const calBarSlot = el('div');
    const calBulkSlot = el('div');
    const calGridSlot = el('div');
    calPane.append(calBarSlot, calBulkSlot, calGridSlot);
    barBuilders.push(() => calBarSlot.replaceChildren(window.SRW_Releases.buildFilterBar(calState, () => renderCalendar(),
      { noun: 'movie', importedStoreKey: 'srw:film-cal-imported', typeTitle: BAR_TYPE_TITLE, providerKeys: ['theporndb'] })));
    let calMonth = null;
    const CAL_STATE = { // film state -> the scene calendar's pill class
      suggested: 'new', wanted: 'sent_to_whisparr', grabbed: 'sent_to_whisparr', import_failed: 'import_failed', imported: 'imported',
    };
    const MAX_PILLS = 3; // per day, like the scene calendar
    const iconSpan = (uri) => el('span', { className: 'srw-cal-pill-whisparr-icon', style: `--srw-icon: url("${uri}")` });
    async function setFilmStatus(f, to) {
      const res = await window.SRW.runOp('films_set_status', { id: f.id, status: to });
      if (res.error) { notify('Movie', res.error, 'error'); return; }
      if (to === 'dismissed') {
        window.SRW.dismissedNotice(`Dismissed: ${f.title}`, '', () => window.SRW.runOp('films_set_status', { id: f.id, status: res.previous }), () => load());
      }
      await load();
    }
    function calPill(f) {
      const stateClass = CAL_STATE[f.status] || 'new';
      const stuck = f.status === 'wanted' && f.last_search_result === 'no_match' ? ' srw-cal-pill-stuck' : '';
      const tpdb = f.status === 'suggested' ? ' srw-cal-pill-theporndb' : ''; // like a scene: the ThePornDB colour only while it is still "new"
      const stateLabel = { suggested: 'New', wanted: 'On Wishlist', grabbed: 'Downloading', import_failed: 'Import failed', imported: 'Imported' }[f.status] || f.status;
      const link = el(f.tpdb_url ? 'a' : 'span', Object.assign({ className: 'srw-cal-pill-link',
        title: `${f.title}${f.year ? ` (${f.year})` : ''} — ${f.studio_name || '?'} (${stateLabel}) [ThePornDB]${f.duration_min ? ` — ${Math.round(f.duration_min)} min` : ''}` },
        f.tpdb_url ? { href: f.tpdb_url, target: '_blank', rel: 'noopener noreferrer' } : {}), [
        f.image_url ? el('img', { className: 'srw-cal-avatar srw-cal-avatar-movie', src: f.image_url, alt: '', loading: 'lazy' }) : null,
        el('span', { className: 'srw-cal-pill-text' }, [
          el('span', { className: 'srw-cal-pill-title', textContent: f.title }),
          el('span', { className: 'srw-cal-pill-studio', textContent: f.studio_name || '' }),
        ]),
      ]);
      const acts = [];
      if (f.status === 'suggested') {
        acts.push(el('button', { className: 'srw-cal-pill-whisparr', title: 'Add to Wishlist',
          onClick: (e) => confirmInline(e.currentTarget, 'Add to Wishlist?', async () => {
            const res = await window.SRW.runOp('films_set_status', { id: f.id, status: 'wanted' });
            if (res.error) { notify('Movie', res.error, 'error'); return; }
            notify('Movie', `"${f.title}" is on the wishlist.`, 'success');
            await load();
          }) }, [iconSpan(window.SRW_Releases.icons.DOWNLOAD)]));
      }
      if (f.status === 'imported' && f.imported_group_id) {
        acts.push(el('a', { className: 'srw-cal-pill-dismiss srw-cal-pill-open', href: `/groups/${f.imported_group_id}`, target: '_blank', rel: 'noopener noreferrer',
          title: 'Open the group in Stash (new tab)', 'aria-label': 'Open group' }, [iconSpan(window.SRW_Releases.icons.OPEN)]));
      }
      if (['suggested', 'wanted', 'import_failed'].includes(f.status)) {
        acts.push(el('button', { className: 'srw-cal-pill-dismiss', title: 'Dismiss', textContent: '×', onClick: () => setFilmStatus(f, 'dismissed') }));
      }
      const pill = el('div', { className: `srw-cal-pill srw-cal-pill-${stateClass}${stuck}${tpdb}` }, [link, el('div', { className: 'srw-cal-pill-actions' }, acts)]);
      pill.dataset.filmId = String(f.id);
      return pill;
    }
    function calLegend() {
      const item = (cls, label) => el('span', { className: 'srw-cal-legend-item' }, [el('span', { className: `srw-cal-legend-swatch srw-cal-pill-${cls}` }), label]);
      return el('div', { className: 'srw-cal-legend' }, [item('new', 'New'), item('sent_to_whisparr', window.SRW.SENT_STATUS_LABEL || 'On Wishlist'),
        item('imported', 'Imported'), item('import_failed', 'Import failed'), item('stuck', 'Searched, nothing usable found'), item('theporndb', 'ThePornDB source')]);
    }
    function renderCalendar() {
      const now = new Date();
      if (!calMonth) calMonth = new Date(now.getFullYear(), now.getMonth(), 1);
      const y = calMonth.getFullYear(); const m = calMonth.getMonth();
      const monthKey = `${y}-${String(m + 1).padStart(2, '0')}`;
      const key = (d) => `${monthKey}-${String(d).padStart(2, '0')}`;
      const inMonth = films.filter((f) => f.release_date && f.release_date.slice(0, 7) === monthKey && f.status !== 'dismissed'
        && (calState.showImported || f.status !== 'imported'));
      const shown = inMonth.filter((f) => passesFilter(f, calState));
      const byDay = {};
      shown.forEach((f) => { (byDay[f.release_date.slice(0, 10)] = byDay[f.release_date.slice(0, 10)] || []).push(f); });
      Object.values(byDay).forEach((d) => d.sort((a, b) => (a.title || '').localeCompare(b.title || '')));
      // "Dismiss N shown": only with an active filter, only the suggestions of this month (what can be seen), with Undo.
      const targets = filterIsActive(calState) ? shown.filter((f) => f.status === 'suggested') : [];
      calBulkSlot.replaceChildren(...(targets.length ? [(() => {
        const btn = el('button', { className: 'srw-btn srw-btn-sm srw-btn-danger', textContent: `Dismiss ${targets.length} shown` });
        btn.addEventListener('click', () => confirmInline(btn, `Dismiss all ${targets.length} movies matching the current filter?`, async () => {
          const ids = targets.map((f) => f.id);
          const res = await window.SRW.runOp('films_bulk_status', { ids, status: 'dismissed' });
          if (res.error) { notify('Bulk dismiss', `Bulk dismiss failed: ${res.error}`, 'error'); return; }
          window.SRW.dismissedNotice(`${res.changed} movie(s) dismissed`, '', () => window.SRW.runOp('films_bulk_status', { ids, status: 'suggested' }), () => load());
          await load();
        }, { danger: true }));
        return btn;
      })()] : []));
      const go = (delta) => { calMonth = delta === 0 ? null : new Date(y, m + delta, 1); renderCalendar(); };
      const header = el('div', { className: 'srw-cal-header' }, [
        el('button', { className: 'srw-btn srw-btn-sm', textContent: '‹ Prev', onClick: () => go(-1) }),
        el('span', { className: 'srw-cal-title', textContent: calMonth.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Next ›', onClick: () => go(1) }),
        el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Today', onClick: () => go(0) }),
      ]);
      const grid = el('div', { className: 'srw-cal-grid' });
      ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].forEach((d) => grid.appendChild(el('div', { className: 'srw-cal-weekday', textContent: d })));
      const lead = (new Date(y, m, 1).getDay() + 6) % 7;
      for (let i = 0; i < lead; i++) grid.appendChild(el('div', { className: 'srw-cal-day srw-cal-day-blank' }));
      const todayKey = new Date().toISOString().slice(0, 10);
      for (let d = 1; d <= new Date(y, m + 1, 0).getDate(); d++) {
        const cell = el('div', { className: `srw-cal-day${key(d) === todayKey ? ' srw-cal-day-today' : ''}` }, [el('div', { className: 'srw-cal-daynum', textContent: String(d) })]);
        const day = byDay[key(d)] || [];
        day.slice(0, MAX_PILLS).forEach((f) => cell.appendChild(calPill(f)));
        if (day.length > MAX_PILLS) {
          const rest = day.slice(MAX_PILLS);
          let open = false; let extra = [];
          const btn = el('button', { className: 'srw-cal-more-btn', textContent: `+${rest.length} more`, onClick: () => {
            if (open) { extra.forEach((n) => n.remove()); extra = []; btn.textContent = `+${rest.length} more`; } else {
              extra = rest.map((f) => { const n = calPill(f); cell.insertBefore(n, btn); return n; }); btn.textContent = 'show less'; }
            open = !open;
          } });
          cell.appendChild(btn);
        }
        grid.appendChild(cell);
      }
      const undated = films.filter((f) => !f.release_date && ['suggested', 'wanted'].includes(f.status)).length;
      calGridSlot.replaceChildren(header, grid, calLegend(),
        ...(undated ? [el('p', { className: 'srw-hint', textContent: `${undated} movie(s) without a release date are not shown here (see the lists).` })] : []));
    }
    let newView = 'calendar';
    try { newView = localStorage.getItem('srw:films-newview') === 'list' ? 'list' : 'calendar'; } catch (e) { /* ignore */ }
    const listModeBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'List' });
    const calModeBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Calendar' });
    const suggestBar = el('div', { className: 'srw-toolbar' }); // "Accept all sure" / "Match unclear again" (filled below)
    const listWrap = el('div', {}, [suggestBar, suggestedList.node]);
    const newPane = el('div', { hidden: true });
    function applyNewView() {
      listModeBtn.classList.toggle('srw-btn-primary', newView === 'list');
      calModeBtn.classList.toggle('srw-btn-primary', newView === 'calendar');
      listWrap.hidden = newView !== 'list';
      calPane.hidden = newView !== 'calendar';
      if (newView === 'calendar') renderCalendar();
      try { localStorage.setItem('srw:films-newview', newView); } catch (e) { /* ignore */ }
    }
    listModeBtn.addEventListener('click', () => { newView = 'list'; applyNewView(); });
    calModeBtn.addEventListener('click', () => { newView = 'calendar'; applyNewView(); });
    newPane.append(el('div', { className: 'srw-subtabs srw-subtabs-inner' }, [listModeBtn, calModeBtn]), listWrap, calPane);
    applyNewView();
    const tabs = window.SRW.buildPaneTabs([
      { key: 'new', label: 'New releases', pane: newPane },
      ...TABS.map(([key, label]) => ({ key, label: () => `${label} (${countOf(key)})`, pane: panes[key], watch: counts })),
      { key: 'groups', label: () => `Multi-file groups${groups ? ` (${groups.length})` : ''}`, pane: groupsPane, watch: counts },
    ], 'srw:films-section', 'new');
    tabs.lastElementChild.addEventListener('click', () => { if (groups === null) loadGroups(); });
    if (!groupsPane.hidden) loadGroups(); // the remembered tab is this one

    // ---------- buttons of the suggestions ----------
    let acceptAllBtn = null;
    let rematchBtn = null;

    // The buttons for the suggestions (list view of "New releases"). The one-time look-ups of the deleted / empty groups are on Maintenance > Movies.
    suggestBar.append(
      (acceptAllBtn = el('button', { className: 'srw-btn', textContent: 'Accept all sure', title: 'Put every suggestion with a sure ThePornDB match on the wishlist',
        onClick: (e) => {
          const ids = films.filter((f) => f.status === 'suggested' && f.match_class === 'sure').map((f) => f.id);
          if (!ids.length) { notify('Accept all sure', 'No sure suggestion left.', 'info'); return; }
          confirmInline(e.currentTarget, `Put all ${ids.length} sure matches on the wishlist?`, async () => {
            const res = await window.SRW.runOp('films_bulk_status', { ids, status: 'wanted' });
            if (res.error) { notify('Accept all sure', res.error, 'error'); return; }
            window.SRW.dismissedNotice(`${res.changed} movie(s) put on the wishlist`, 'They are on the On Wishlist tab now.',
              () => window.SRW.runOp('films_bulk_status', { ids, status: 'suggested' }), () => load());
            await load();
          });
        } })),
      (rematchBtn = el('button', { className: 'srw-btn', textContent: 'Match unclear again',
        title: 'Looks the unclear suggestions (likely / unsure / no match) up on ThePornDB again and keeps the best candidates -- one request each',
        onClick: (e) => {
          const n = films.filter((f) => f.status === 'suggested' && ['likely', 'unsure', 'none'].includes(f.match_class)).length;
          if (!n) { notify('Match again', 'No unclear suggestion left.', 'info'); return; }
          confirmInline(e.currentTarget, `Look the ${n} unclear suggestions up on ThePornDB again? One request each (${n}), in the background.`, async () => {
            const res = await window.SRW.runOp('films_rematch');
            if (res.error) { notify('Match again', res.error, 'error'); return; }
            trackSeed(status, () => load());
          });
        } })),
      status,
    );

    root.append(tabs, newPane, ...TABS.map(([k]) => panes[k]), groupsPane, counts);
    window.SRW.getSettings().then((st) => { sidecarUrl = (st && st.sidecarUrl) || ''; }).catch(() => {}).finally(() => { load(); trackSeed(status, () => load()); });
    window.SRW.runOp('settings_get_all').then((st) => { if (st && !st.error) window.SRW_Releases.loadFilterPresets(st); }).catch(() => {})
      .finally(() => barBuilders.forEach((build) => build()));
    root.reload = load;
    return root;
  }

  // Progress of the background look-up on ThePornDB (seed match / match again), shown in `statusEl`; `onDone` when it finished.
  let seedTimer = null;
  async function trackSeed(statusEl, onDone) {
    clearTimeout(seedTimer);
    const st = await window.SRW.runOp('films_seed_match_status');
    if (!st || st.error) { statusEl.textContent = ''; return; }
    if (st.running) {
      statusEl.textContent = ` ${st.source === 'rematch' ? 'Matching the unclear ones again' : 'Looking up movies on ThePornDB'}: ${st.done} / ${st.total} (${st.requests} request(s) so far)…`;
      seedTimer = setTimeout(() => trackSeed(statusEl, onDone), 3000);
    } else if (st.total) {
      const parts = Object.entries(st.added || {}).map(([k, n]) => `${n}× ${(CLASS_LABEL[k] || [k])[0]}`).join(', ');
      statusEl.textContent = ` ${st.source === 'rematch' ? 'Match again' : 'Look-up'} finished: ${st.requests} request(s) -- ${parts || 'nothing new'}${st.error ? ` (last error: ${st.error})` : ''}.`;
      if (onDone) await onDone();
    }
  }

  // One-time look-ups for Maintenance > Movies (moved here from the movie page, which now looks like the scene page): the movies we only know
  // by name -- the groups deleted on 2026-10-04 and the empty groups in Stash -- are looked up on ThePornDB (one request each) and arrive
  // as suggestions on Movies > New releases.
  function buildSeedTools() {
    const { el, notify, confirmInline } = window.SRW;
    const statusEl = el('span', { className: 'srw-key-status' });
    const btn = (label, source, question) => el('button', { className: 'srw-btn', textContent: label,
      title: 'Looks the movies up on ThePornDB (one request per movie) and lists them as suggestions -- nothing is wanted until you accept it',
      onClick: (e) => confirmInline(e.currentTarget, question, async () => {
        const res = await window.SRW.runOp('films_seed_match', { source });
        if (res.error) { notify('Look-up', res.error, 'error'); return; }
        trackSeed(statusEl);
      }) });
    const node = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: 'Movie wishlist: look up movies on ThePornDB' }),
      el('p', { className: 'srw-hint', textContent: 'For movies we only know by name. One ThePornDB request per movie, in the background; the results arrive as suggestions on Movies > New releases and nothing is wanted until you accept it.' }),
      el('div', { className: 'srw-toolbar' }, [
        btn('Match deleted groups', 'deleted-groups', 'Look up the 72 movies of the deleted groups on ThePornDB? One request per movie (72), in the background.'),
        btn('Match empty Stash groups', 'empty-groups', 'Look up every empty group in Stash on ThePornDB? One request per group (about 100), in the background.'),
        statusEl,
      ]),
    ]);
    trackSeed(statusEl);
    return node;
  }

  // Maintenance > Movies & groups: bring the movie wishlist into Whisparr (v0.140.0). (Moved to the Movies page in v0.141.0, back in v0.142.0:
  // new movies are registered automatically, this is only the catch-up / repair tool.) Whisparr is told about the movies per the Whisparr scope preset like
  // for the scenes (bookkeeping only / Whisparr searches / full Whisparr; nothing under "No Whisparr"). A dry run first, then apply.
  // One block for a Whisparr job of the movies (dry run + apply + result list), used by both the registration sync and the folder report (stage B).
  // `cfg`: heading, how (explanation), startOp / statusOp (plugin ops), summarize(r) -> sentence, lists(r) -> [[title, items]], applyQuestion,
  // inertIn (presets that make it inert), inertReason.
  function buildWhisparrJobBlock(cfg) {
    const { el, notify, confirmInline } = window.SRW;
    const out = el('div');
    const status = el('span', { className: 'srw-key-status' });
    let timer = null;
    const list = (title, items) => (items && items.length ? el('div', { className: 'srw-hint', textContent: `${title}: ${items.slice(0, 12).join(', ')}${items.length > 12 ? ` … (+${items.length - 12})` : ''}` }) : null);
    function show(st) {
      clearTimeout(timer);
      if (!st || st.error) { status.textContent = st && st.error ? ` error: ${st.error}` : ''; return; }
      if (st.running) { status.textContent = ' working…'; timer = setTimeout(poll, 3000); return; }
      status.textContent = '';
      const r = st.result;
      if (!r) { out.replaceChildren(); return; }
      if (r.skipped) { out.replaceChildren(el('p', { className: 'srw-hint', textContent: r.skipped })); return; }
      const problems = cfg.problemCount(r);
      out.replaceChildren(
        el('p', { className: problems ? 'srw-search-status' : 'srw-hint', textContent: `${r.dry_run ? 'Check (nothing was changed)' : 'Done'} -- preset ${r.preset}: ${cfg.summarize(r)}` }),
        ...cfg.lists(r).map(([title, items]) => list(title, items)).filter(Boolean));
    }
    async function poll() { show(await window.SRW.runOp(cfg.statusOp)); }
    const start = async (dry) => {
      const res = await window.SRW.runOp(cfg.startOp, { dry_run: dry });
      if (res.error) { notify('Whisparr', res.error, 'error'); return; }
      status.textContent = ' working…';
      timer = setTimeout(poll, 2000);
    };
    const checkBtn = el('button', { className: 'srw-btn', textContent: 'Check (dry run)', title: `Dry run: nothing is written.\n\n${cfg.how}`, onClick: () => start(true) });
    const applyBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: cfg.applyLabel || 'Sync now', title: cfg.how,
      onClick: (e) => confirmInline(e.currentTarget, cfg.applyQuestion, () => start(false)) });
    const node = el('div', { className: 'srw-settings-section' }, [
      el('h4', { textContent: cfg.heading }),
      el('p', { className: 'srw-hint', textContent: cfg.how }),
      el('div', { className: 'srw-toolbar' }, [checkBtn, applyBtn, status]), out]);
    // Greyed out (with the reason) when the job does nothing under the effective Whisparr scope (the backend reports it).
    const scopeState = window.SRW.createScopeStateRegistry();
    scopeState.add(node, cfg.inertIn, cfg.inertReason);
    window.SRW.runOp('settings_search_backend').then((info) => { if (info && !info.error) scopeState.apply(info.scope_preset); }).catch(() => {});
    poll();
    return node;
  }

  function buildWhisparrSyncTools() {
    const how = 'Whisparr has a movie area of its own (ThePornDB movies, one file per movie). The wishlist movies are registered there following the Whisparr scope preset (Settings > General > Workflow): '
      + 'bookkeeping only = unmonitored, nothing is searched; Whisparr searches / full Whisparr = monitored for that one movie; No Whisparr (or not configured) = Whisparr is ignored. '
      + 'New wishlist movies are registered automatically; this catches up the ones that were there before. Dry run: one Whisparr lookup per movie that is not in Whisparr yet; nothing is written.';
    return buildWhisparrJobBlock({
      heading: 'Movie wishlist: sync with Whisparr', how, startOp: 'films_whisparr_sync', statusOp: 'films_whisparr_status', applyLabel: 'Sync now',
      applyQuestion: 'Register the movies in Whisparr now (per the Whisparr scope preset)? Nothing is deleted.',
      problemCount: (r) => (r.errors || []).length + (r.conflict || []).length,
      summarize: (r) => `${r.checked} movie(s) looked up; ${r.register.length} to register in Whisparr, ${r.link.length} already there (${r.set_monitored.length} with another monitoring), `
        + `${r.not_found.length} not found, ${(r.adopt || []).length} use Whisparr's entry of the same movie (other id), ${(r.conflict || []).length} conflict(s), ${r.no_tpdb} without a ThePornDB id, ${r.errors.length} error(s).`,
      lists: (r) => [['Register', r.register], ['Already in Whisparr', r.link], ['Same movie under another id (Whisparr\'s entry is used)', r.adopt], ['Conflict (decide by hand)', r.conflict],
        ['Monitoring changes', r.set_monitored], ['Not found in Whisparr', r.not_found], ['Errors', r.errors]],
      inertIn: ['no_whisparr'],
      inertReason: 'Not used: Whisparr is not configured, or the Whisparr scope is “No Whisparr” (Settings > General > Workflow) -- nothing is registered in Whisparr.',
    });
  }

  // Stage C (v0.159.0): where Whisparr searches (Search + Sidecar-Import, Full Whisparr) take over what it grabbed / imported -- ONE history request.
  function buildWhisparrHistoryTools() {
    const how = 'Where Whisparr searches for the movies (Settings > General > Workflow: “Whisparr searches + downloads, this sidecar imports” or “Full Whisparr management”), this program learns what Whisparr did from ONE '
      + 'history request: a grab is followed to its download and imported here; with Full Whisparr the movie Whisparr imported gets its Group in Stash. Whisparr is only read. It runs every hour by itself; '
      + 'the first run only sets the starting point (older events are not taken over). Dry run: shows what would be taken over from the last 7 days (or since the last run); nothing is written.';
    return buildWhisparrJobBlock({
      heading: 'Whisparr history: take over grabs and imports', how, startOp: 'films_whisparr_history_check', statusOp: 'films_whisparr_history_status', applyLabel: 'Check now',
      applyQuestion: 'Take over what Whisparr grabbed / imported for the movies now? Whisparr is only read.',
      problemCount: (r) => (r.errors || []).length,
      summarize: (r) => (r.first_run ? r.note : `${r.events} Whisparr event(s) looked at; ${r.grab.length} grab(s) ${r.dry_run ? 'would be' : 'were'} taken over, ${r.import.length} import(s), ${r.ignored} not ours or not relevant, ${r.errors.length} problem(s).`),
      lists: (r) => [[r.dry_run ? 'Would take over (grabbed)' : 'Grabbed', r.grab], [r.dry_run ? 'Would take over (imported)' : 'Imported', r.import], ['Problems', r.errors]],
      inertIn: ['no_whisparr', 'bookkeeping_only'],
      inertReason: 'Not used: Whisparr searches for the movies only with “Whisparr searches + downloads, this sidecar imports” or “Full Whisparr management”.',
    });
  }

  // Stage B (v0.158.0): tell Whisparr WHERE an imported movie lies and let it take the file over. Nothing is moved, renamed or deleted.
  function buildWhisparrFileTools() {
    const how = 'After an import the movie lies in its own folder; Whisparr does not know that yet. This tells Whisparr the folder (it moves nothing: the path is only set) and lets it re-read the folder, '
      + 'so the movie shows as “has a file” there. A folder that already belongs to ANOTHER Whisparr movie is only reported, nothing is written. Automatically for new imports once '
      + '“Tell Whisparr the folder of imported movies” is on (Settings > General > Workflow); this catches up the movies that were imported before. '
      + 'Dry run: one Whisparr request per movie (plus one list of all movies if a folder has to be compared); nothing is written. Applying: about 6 requests per movie.';
    return buildWhisparrJobBlock({
      heading: 'Imported movies: tell Whisparr the folder', how, startOp: 'films_whisparr_files_sync', statusOp: 'films_whisparr_files_status', applyLabel: 'Apply',
      applyQuestion: 'Tell Whisparr the folder of the imported movies now? Whisparr moves, renames and deletes nothing.',
      problemCount: (r) => (r.conflict || []).length + (r.not_taken || []).length + (r.no_folder || []).length + (r.errors || []).length,
      summarize: (r) => `${r.checked} imported movie(s) looked at; ${r.set_path.length} ${r.dry_run ? 'would be reported' : 'reported'}, ${r.already.length} already right, `
        + `${r.conflict.length} folder owned by another Whisparr movie, ${r.not_taken.length} not taken over, ${r.no_folder.length + r.errors.length} problem(s).`,
      lists: (r) => [[r.dry_run ? 'Would report' : 'Reported', r.set_path], ['Already right', r.already], ['Folder owned by another movie (nothing written)', r.conflict],
        ['Whisparr did not take the file', r.not_taken], ['No folder found', r.no_folder], ['Errors', r.errors]],
      inertIn: ['no_whisparr', 'full_whisparr'],
      inertReason: 'Not used: Whisparr is not configured, the scope is “No Whisparr”, or “Full Whisparr management” (Whisparr imports the movies itself) -- nothing is reported.',
    });
  }

  window.SRW_Films = { buildPane, buildTpdbSearch, buildSeedTools, buildWhisparrSyncTools, buildWhisparrFileTools, buildWhisparrHistoryTools };
})();
