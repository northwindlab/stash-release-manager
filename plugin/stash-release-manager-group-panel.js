/*
 * "Releases" tab on Stash's GROUP pages (v0.119.0, named like the tab on Performer/Studio pages): the movie of one group.
 * Look the movie up on StashDB as scenes and put the missing ones on the scene wishlist FOR THIS GROUP, search the whole movie as one
 * file, link the ThePornDB movie. (Series list, metadata for the group and scene names follow.)
 */
(function () {
  'use strict';

  const TAB_ID = 'srw-group-tab';
  const PANE_ID = 'srw-group-pane';
  const HASH = 'srm-releases'; // same name as on the Performer/Studio pages
  let token = 0;
  let watchRoute = null; // the group route whose tab is kept mounted (v0.124.1)
  let watchTimer = null;
  let injecting = false;

  function clear() {
    watchRoute = null;
    document.getElementById(TAB_ID)?.remove();
    document.getElementById(PANE_ID)?.remove();
  }

  // Stash rebuilds the tab strip after leaving the edit form (Edit > Cancel/Save) and drops our foreign tab: put it back while the
  // same group page is still open. Debounced; nothing happens while an inject is running or on another page.
  new MutationObserver(() => {
    if (!watchRoute || injecting || document.getElementById(TAB_ID)) return;
    clearTimeout(watchTimer);
    watchTimer = setTimeout(() => {
      const route = watchRoute;
      if (!route || injecting || document.getElementById(TAB_ID)) return;
      if (!new RegExp(`^/groups/${route.id}(/|$)`).test(location.pathname)) return;
      if (!document.querySelector('a[id^="group-tabs-tab-"]')) return; // edit form open: Stash shows no tab strip
      inject(route);
    }, 400);
  }).observe(document.body, { childList: true, subtree: true });

  function buildMoviePanel(groupId) {
    const { el, notify, confirmInline } = window.SRW;
    const root = el('details', { className: 'srw-settings-section srw-panel', id: 'srw-group-movie' }, [el('summary', { textContent: 'This movie' })]);
    const body = el('div');
    root.appendChild(body);
    let info = null;
    let movie = null;

    async function load() {
      const res = await window.SRW.runOp('films_group_info', { group_id: groupId });
      if (res.error) { body.textContent = `Error: ${res.error}`; return; }
      info = res.group;
      movie = res.movie;
      render();
    }

    const minutes = (n) => `${Math.round(n)} min`;

    function movieStatusLine() {
      if (!movie) return 'Not on the Movies wishlist.';
      if (movie.status === 'grabbed' && movie.mode === 'scenes') return `Being put together from StashDB scenes (${(movie.scene_ids || []).length} on the scene wishlist) -- the new scenes join this group when they are imported.`;
      if (movie.status === 'grabbed') return movie.pending_import_path ? 'The downloaded file is in the library, waiting for Stash and the group.' : 'A download is running for this movie.';
      const label = { wanted: 'on the Movies wishlist', suggested: 'a suggestion', imported: 'imported', import_failed: 'import failed', dismissed: 'dismissed' }[movie.status] || movie.status;
      return `Movies wishlist: ${label}${movie.import_error ? ` -- ${movie.import_error}` : ''}.`;
    }

    // The film row this group hangs on (created on first need).
    async function ensureMovie() {
      if (movie) return movie;
      const res = await window.SRW.runOp('films_group_want', { group_id: groupId });
      if (res.error) { notify('Movie', res.error, 'error'); return null; }
      movie = res.movie;
      return movie;
    }

    function render() {
      body.innerHTML = '';
      const facts = [info.date, info.studio, `${info.scenes} scene${info.scenes === 1 ? '' : 's'}`, `${info.files} file${info.files === 1 ? '' : 's'}`,
        info.file_minutes ? `${minutes(info.file_minutes)} in files` : null, info.group_minutes ? `group says ${minutes(info.group_minutes)}` : null].filter(Boolean);
      const panelSlot = el('div');
      const checkSlot = el('div');
      body.append(...[
        el('p', { className: 'srw-hint', textContent: facts.join(' — ') }),
        el('p', { className: 'srw-hint', textContent: movieStatusLine() }),
        movie && movie.tpdb_url ? el('p', { className: 'srw-hint' }, ['ThePornDB movie: ',
          el('a', { href: movie.tpdb_url, target: '_blank', rel: 'noopener noreferrer', textContent: `${movie.title}${movie.year ? ` (${movie.year})` : ''} ↗` })]) : null,
      ].filter(Boolean));
      const actions = el('div', { className: 'srw-toolbar' });

      // 1) StashDB scenes
      actions.appendChild(window.SRW.iconButton('button', ['faMagnifyingGlass', 'faList'], 'Check StashDB scenes', { className: 'srw-btn srw-btn-primary',
        title: 'Is this movie on StashDB as scenes? One StashDB request; nothing is changed',
        onClick: async () => {
          checkSlot.innerHTML = '';
          checkSlot.appendChild(el('p', { className: 'srw-hint', textContent: 'Looking the movie up on StashDB (one request)…' }));
          const res = await window.SRW.runOp('films_group_check', { group_id: groupId });
          checkSlot.innerHTML = '';
          if (res.error) { notify('StashDB check', res.error, 'error'); return; }
          const missing = (res.scenes || []).filter((c) => !c.owned);
          const lines = (res.scenes || []).map((x) => el('div', { className: 'srw-manual-search-result' }, [
            el('span', { className: 'srw-release-title', textContent: `Scene ${x.k}` }), el('span', { textContent: x.title }),
            el('span', { className: 'srw-review-candidate-meta', textContent: [x.studio, x.date, x.minutes ? minutes(x.minutes) : null].filter(Boolean).join(' — ') }),
            x.owned ? el('span', { className: 'srw-film-class srw-film-class-sure', textContent: 'in this group', title: 'A scene of this group already carries this StashDB scene' })
              : el('span', { className: 'srw-film-class srw-film-class-unsure', textContent: 'missing' })]));
          checkSlot.append(...[
            el('p', { className: missing.length ? 'srw-search-status' : 'srw-hint',
              textContent: `${res.message}${(res.scenes || []).length ? ` ${missing.length} of ${(res.scenes || []).length} are not in this group yet.` : ''}` }),
            ...lines,
            missing.length ? el('button', { className: 'srw-btn srw-btn-primary', textContent: `Put the ${missing.length} missing scene${missing.length === 1 ? '' : 's'} on the scene wishlist`,
              title: 'They are searched, downloaded and verified like any wishlist scene and join THIS group when imported',
              onClick: (e) => confirmInline(e.currentTarget,
                `Put ${missing.length} scene(s) of "${info.name}" on the scene wishlist for this group?`, async () => {
                  const done = await window.SRW.runOp('films_group_use_scenes', { group_id: groupId });
                  if (done.error) { notify('Scenes', done.error, 'error'); return; }
                  notify('Scenes', `${done.scenes} scene(s) are on the scene wishlist (${done.new} new). They join this group when imported.`, 'success');
                  await load();
                }) }) : null,
          ].filter(Boolean));
        } }));

      // 2) the whole movie as ONE file
      if (!movie || ['wanted', 'suggested', 'import_failed'].includes(movie.status)) {
        actions.appendChild(el('button', { className: 'srw-btn', textContent: 'Search as one file',
          title: 'Search the indexers for the whole movie as a single download; when imported it joins this group',
          onClick: async () => {
            const row = await ensureMovie();
            if (!row) return;
            panelSlot.innerHTML = '';
            const caps = await window.SRW.getSearchCapabilities();
            const sources = (caps && caps.manual_sources) || [];
            if (!sources.length) { notify('Search', 'No search source (Prowlarr / direct indexers) is configured.', 'error'); return; }
            const asRelease = { provider: 'theporndb', scene_stashdb_id: row.tpdb_id || `film-${row.id}`, title: `${row.title}${row.year ? ` ${row.year}` : ''}` };
            const search = window.SRW.buildManualSearch(asRelease, sources, caps.backend, () => load(), {
              host: () => panelSlot,
              grab: (candidate) => window.SRW.runOp('films_grab', { id: row.id, candidate }),
            });
            panelSlot.appendChild(search);
            search.querySelector('button').click(); // opens the search panel
          } }));
      }

      // 3) the ThePornDB movie
      if (!movie || ['wanted', 'suggested', 'import_failed'].includes(movie.status)) {
        actions.appendChild(el('button', { className: 'srw-btn', textContent: movie && movie.tpdb_id ? 'Change ThePornDB movie…' : 'Link ThePornDB movie…',
          title: 'Pick the ThePornDB movie this group is: title, studio, length and cover come from it when the movie is imported',
          onClick: async () => {
            const row = await ensureMovie();
            if (!row) return;
            panelSlot.innerHTML = '';
            const panel = window.SRW_Films.buildTpdbSearch({
              initial: info.name, actionLabel: 'Use this movie',
              onPick: async (m) => {
                const res = await window.SRW.runOp('films_link', { id: row.id, movie: m.record });
                if (res.error) { notify('Link movie', res.error, 'error'); return; }
                notify('Link movie', `This group is linked to "${m.title}".`, 'success');
                await load();
              },
            });
            panelSlot.appendChild(panel.node);
            panel.run();
          } }));
      }
      // 4) the ThePornDB data on THIS group (v0.123.0)
      if (movie && movie.tpdb_id) {
        actions.appendChild(el('button', { className: 'srw-btn', textContent: 'Apply ThePornDB data to this group…',
          title: 'Compare this group with the linked ThePornDB movie and copy the fields you tick (a backup is written first, Undo is offered)',
          onClick: () => openMetaPanel(panelSlot) }));
      }
      // 5) scene names + order from our scrapers: AdultDVDEmpire (titles, lengths) and IAFD (performers per scene, bonus scenes) (v0.124.0/v0.127.0)
      actions.appendChild(el('button', { className: 'srw-btn', textContent: 'Scene names & order…',
        title: 'Reads the scene list of this movie from AdultDVDEmpire or IAFD (one request to that site) and lets you name this group\'s scenes and put them in order',
        onClick: () => openNamesPanel(panelSlot, '') }));
      body.append(actions, checkSlot, panelSlot);
    }


    // Preview (no request) -> tick fields -> backup + apply -> Undo
    async function openMetaPanel(panelSlot) {
      panelSlot.innerHTML = '';
      panelSlot.appendChild(el('p', { className: 'srw-hint', textContent: 'Comparing with the ThePornDB movie…' }));
      const res = await window.SRW.runOp('films_group_meta_preview', { group_id: groupId });
      panelSlot.innerHTML = '';
      if (res.error) { notify('ThePornDB data', res.error, 'error'); return; }
      const boxes = {};
      const rows = res.rows.map((r) => {
        const box = el('input', { type: 'checkbox' });
        box.checked = !!r.checked;
        box.disabled = !r.has_new;
        boxes[r.key] = box;
        const note = !r.has_new ? 'ThePornDB has nothing' : (!r.differs ? 'same' : (r.checked ? 'fills the empty field' : 'differs'));
        return el('label', { className: 'srw-manual-search-result', style: 'display:grid;grid-template-columns:auto 9em 1fr 1fr auto;gap:.6rem;align-items:start' }, [
          box, el('strong', { textContent: r.label }),
          el('span', { className: 'srw-review-candidate-meta', textContent: r.current ? String(r.current) : '—' }),
          el('span', { textContent: r.new ? String(r.new) : '—' }),
          el('span', { className: 'srw-hint', textContent: note }),
        ]);
      });
      const apply = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Apply ticked fields',
        onClick: (e) => confirmInline(e.currentTarget, 'Copy the ticked ThePornDB fields to this group? A backup is written first.', async () => {
          const keys = Object.keys(boxes).filter((k) => boxes[k].checked);
          if (!keys.length) { notify('ThePornDB data', 'Nothing is ticked.', 'info'); return; }
          const done = await window.SRW.runOp('films_group_meta_apply', { group_id: groupId, keys });
          if (done.error) { notify('ThePornDB data', done.error, 'error'); return; }
          panelSlot.innerHTML = '';
          window.SRW.dismissedNotice(`Group updated (${done.applied.fields.join(', ')})`, `Backup: ${done.backup}`,
            () => window.SRW.runOp('films_group_meta_undo', { applied: done.applied }), () => load());
          await load();
        }) });
      panelSlot.append(...[
        el('p', { className: 'srw-hint', textContent: 'Left: this group now. Right: the linked ThePornDB movie. Empty fields are pre-ticked; the name and existing covers only change if you tick them. URLs are added.' }),
        ...rows,
        (res.notes || []).length ? el('p', { className: 'srw-hint', textContent: res.notes.join(' — ') }) : null,
        apply,
      ].filter(Boolean));
    }


    // Scene names: AdultDVDEmpire list -> per local scene pick a name (titles only) -> backup + apply (films_group_assign) -> Undo
    async function openNamesPanel(panelSlot, source) {
      panelSlot.innerHTML = '';
      panelSlot.appendChild(el('p', { className: 'srw-hint', textContent: 'Reading the movie page (one request)…' }));
      const res = await window.SRW.runOp('films_group_scene_names', { group_id: groupId, source });
      panelSlot.innerHTML = '';
      if (res.error) { notify('Scene names', res.error, 'error'); return; }
      const sourceSelect = el('select', { className: 'srw-filter-preset-select' },
        (res.sources || []).map((x) => el('option', { value: x.key, textContent: `${x.label}${x.available ? '' : ' (not available)'}` })));
      sourceSelect.value = res.source;
      sourceSelect.addEventListener('change', () => openNamesPanel(panelSlot, sourceSelect.value));
      const header = el('div', { className: 'srw-toolbar' }, [el('span', { className: 'srw-hint', textContent: 'Source: ' }), sourceSelect]);
      const known = res.scenes || [];
      if (!known.length) { panelSlot.append(header, el('p', { className: 'srw-hint', textContent: res.message })); return; }
      const label = (k) => (res.source === 'iafd'
        ? `${k.kind === 'bonus' ? 'Bonus' : 'Scene'} ${k.n}: ${(k.performers || []).join(', ')}`
        : `Scene ${k.n}: ${k.title}${k.minutes ? ` (${k.minutes} min)` : ''}`);
      const rows = (info.scene_list || []).map((sc) => {
        const select = el('select', { className: 'srw-filter-preset-select' }, [
          el('option', { value: '', textContent: 'keep as it is' }),
          ...known.map((k) => el('option', { value: String(k.n), textContent: label(k) })),
          el('option', { value: 'm', textContent: 'my own name…' }),
        ]);
        const input = el('input', { type: 'text', className: 'srw-search-input', value: sc.title || '', hidden: true });
        const sync = () => {
          const k = known.find((x) => String(x.n) === select.value);
          if (k && k.title) input.value = k.title; // IAFD has no titles: the name stays as it is unless you type one
          input.hidden = !select.value || (!!k && !k.title);
        };
        select.addEventListener('change', sync);
        const hint = res.suggested && res.suggested[sc.id];
        if (!sc.title && hint) select.value = String(hint); // only scenes without a title are pre-filled
        sync();
        return { sc, select, input, node: el('div', { className: 'srw-manual-search-result srw-group-scene' }, [
          el('a', { className: 'srw-vr-thumb-link', href: `/scenes/${sc.id}`, target: '_blank', rel: 'noopener noreferrer', title: 'Open the scene in Stash (new tab) to watch it' },
            [el('img', { className: 'srw-vr-thumb', src: `/scene/${sc.id}/screenshot`, loading: 'lazy', alt: '' })]),
          el('div', {}, [
            el('a', { className: 'srw-release-title', href: `/scenes/${sc.id}`, target: '_blank', rel: 'noopener noreferrer', textContent: sc.title || '(no title)' }),
            el('div', { className: 'srw-hint', textContent: `${sc.file} — ${Math.round(sc.minutes)} min${hint ? ` — length fits Scene ${hint}` : ''}` }),
          ]),
          select, input]) };
      });
      const orderBox = el('input', { type: 'checkbox' });
      orderBox.checked = true;
      const apply = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Apply names',
        onClick: (e) => {
          const numeric = (r) => /^\d+$/.test(r.select.value);
          const chosen = rows.filter((r) => r.select.value && (numeric(r) || r.input.value.trim()));
          const withOrder = orderBox.checked && chosen.some(numeric);
          const items = chosen.map((r) => ({ scene_id: r.sc.id, ...(!r.input.hidden && r.input.value.trim() ? { title: r.input.value.trim() } : {}),
            ...(withOrder && numeric(r) ? { scene_index: Number(r.select.value) } : {}) })).filter((x) => x.title || x.scene_index != null);
          if (withOrder) { // scenes without a list position go last, in the order shown
            let next = Math.max(known.length, ...items.map((x) => x.scene_index || 0)) + 1;
            rows.forEach((r) => {
              const it = items.find((x) => String(x.scene_id) === String(r.sc.id));
              if (!it) items.push({ scene_id: r.sc.id, scene_index: next++ });
              else if (it.scene_index == null) it.scene_index = next++;
            });
          }
          if (!items.length) { notify('Scene names', 'Nothing chosen.', 'info'); return; }
          const nTitles = items.filter((x) => x.title).length;
          confirmInline(e.currentTarget, `Set ${nTitles} scene title${nTitles === 1 ? '' : 's'}${withOrder ? ' and the order of the scenes in the group' : ''} in Stash? A backup is written first.`, async () => {
            const done = await window.SRW.runOp('films_group_assign', { group_id: groupId, items });
            if (done.error) { notify('Scene names', done.error, 'error'); return; }
            if (done.errors && done.errors.length) notify('Scene names', `${done.errors.length} failed: ${done.errors.map((x) => `scene ${x.scene_id}: ${x.error}`).join('; ')}`, 'error');
            if (done.applied.length) window.SRW.dismissedNotice(`${done.applied.length} scene(s) updated`, `Backup: ${done.backup}`,
              () => window.SRW.runOp('films_group_assign_undo', { applied: done.applied }), () => load());
            await load();
          });
        } });
      panelSlot.append(...[
        header,
        el('p', { className: 'srw-hint', textContent: `${res.message} Only titles and the position in the group are set (nothing else of the scene changes). Scenes that already have a title stay on "keep as it is" until you pick a name.` }),
        el('p', { className: 'srw-hint' }, ['Source: ', el('a', { href: res.url, target: '_blank', rel: 'noopener noreferrer', textContent: res.url })]),
        ...rows.map((r) => r.node),
        el('label', { className: 'srw-hint', style: 'display:block;margin:.4rem 0' }, [orderBox, ' Also set the order of the scenes in the group: the number of the chosen list entry; scenes without a name go last.']),
        apply,
      ]);
    }

    load();
    return root;
  }

  // ---------- Series: every movie of the series this group belongs to (v0.120.0) ----------
  function buildSeriesPanel(groupId) {
    const { el, notify, confirmInline } = window.SRW;
    const root = el('details', { className: 'srw-settings-section srw-panel', id: 'srw-group-series' }, [el('summary', { textContent: 'Series' })]);
    const status = el('p', { className: 'srw-hint', textContent: 'All movies of this series on ThePornDB, marked as in your library, on the Movies wishlist or available. One ThePornDB request.' });
    const listSlot = el('div');
    let items = [];

    const STATE = {
      library: ['in the library', 'srw-film-class-sure'], wishlist: ['on the wishlist', 'srw-film-class-likely'],
      'empty-group': ['empty group in Stash', 'srw-film-class-unsure'], available: ['available', ''],
    };

    async function addIds(ids, label) {
      const res = await window.SRW.runOp('films_group_series_add', { group_id: groupId, tpdb_ids: ids });
      if (res.error) { notify('Series', res.error, 'error'); return; }
      if (res.added.length) {
        window.SRW.dismissedNotice(`${res.added.length} movie(s) put on the Movies wishlist`, label || '',
          () => window.SRW.runOp('films_remove_new', { ids: res.added }), () => loadSeries());
      } else {
        notify('Series', res.existing ? 'They are already on the wishlist.' : 'Nothing added.', 'info');
      }
      await loadSeries();
    }

    function render(res) {
      listSlot.innerHTML = '';
      items = res.items || [];
      const addable = items.filter((i) => i.state === 'available' || i.state === 'empty-group');
      listSlot.append(...[
        el('p', { className: 'srw-hint', textContent: `${items.length} movie(s) of "${res.series}" on ThePornDB: ${items.filter((i) => i.state === 'library').length} in your library, `
          + `${items.filter((i) => i.state === 'wishlist').length} on the wishlist, ${addable.length} not yet.${res.more ? ' (There may be more -- the lookup is limited to 50.)' : ''}` }),
        addable.length ? el('button', { className: 'srw-btn srw-btn-primary', textContent: `Add all missing (${addable.length})`,
          title: 'Put every movie that is not in the library and not on the wishlist on the Movies wishlist',
          onClick: (e) => confirmInline(e.currentTarget, `Put ${addable.length} movie(s) of this series on the Movies wishlist?`, () => addIds(addable.map((i) => i.tpdb_id))) }) : null,
        el('ul', { className: 'srw-release-list' }, items.map((i) => {
          const [label, cls] = STATE[i.state] || [i.state, ''];
          return el('li', { className: 'srw-release-item srw-film-row' }, [
            i.image_url ? el('img', { className: 'srw-film-thumb srw-film-thumb-sm', src: i.image_url, loading: 'lazy', alt: '' }) : null,
            el('div', { className: 'srw-row-info' }, [
              el('div', {}, [el('span', { className: 'srw-release-title', textContent: i.title }),
                el('span', { className: `srw-film-class ${cls}`, textContent: label })]),
              el('div', { className: 'srw-hint' }, [
                el('span', { textContent: [i.year, i.site, i.minutes ? `${Math.round(i.minutes)} min` : null,
                  i.folded ? `+${i.folded} thinner ThePornDB entr${i.folded === 1 ? 'y' : 'ies'} of this number hidden` : null].filter(Boolean).join(' — ') }),
                i.url ? el('a', { href: i.url, target: '_blank', rel: 'noopener noreferrer', textContent: '  ThePornDB ↗' }) : null,
                i.group_id ? el('a', { href: `/groups/${i.group_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: '  Group in Stash →' }) : null].filter(Boolean)),
            ]),
            el('div', { className: 'srw-row-actions' }, [
              i.state === 'available' || i.state === 'empty-group' ? el('button', { className: 'srw-btn', textContent: 'Add to wishlist',
                onClick: () => addIds([i.tpdb_id]) }) : null].filter(Boolean)),
          ].filter(Boolean));
        })),
      ].filter(Boolean));
    }

    async function loadSeries() {
      status.textContent = 'Looking the series up on ThePornDB (one request)…';
      const res = await window.SRW.runOp('films_group_series', { group_id: groupId });
      if (res.error) { status.textContent = `Error: ${res.error}`; return; }
      status.textContent = '';
      render(res);
    }

    root.append(status, el('div', { className: 'srw-toolbar' }, [el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Load the series',
      title: 'One ThePornDB request', onClick: () => loadSeries() })]), listSlot);
    return root;
  }

  async function inject(route) {
    const myToken = ++token;
    clear();
    injecting = true;
    try {
      const content = await window.SRW.waitForElement('.tab-content', { timeout: 6000 });
      if (myToken !== token || !content) return;
      const tabLink = document.querySelector('a[id^="group-tabs-tab-"]');
      const nav = tabLink && tabLink.closest('.nav-tabs');
      if (!nav || !window.SRW_EntityPanel) return;
      clear();
      await window.SRW.loadMetadataProviderUrls();
      if (myToken !== token) return;
      window.SRW_EntityPanel.mountTab(nav, content, [buildMoviePanel(route.id), buildSeriesPanel(route.id)], { tabId: TAB_ID, paneId: PANE_ID, hash: HASH, label: 'Releases' });
      watchRoute = route;
    } finally {
      if (myToken === token) injecting = false;
    }
  }

  window.SRW_GroupPanel = { inject, clear };
})();
