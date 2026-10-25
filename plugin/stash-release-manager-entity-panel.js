/**
 * StashReleaseManager - Entity panel
 * Injects a "new releases" section into Stash's own Studio and Performer
 * detail pages, scoped to that entity, per PROGRESS.md's 2026-09-11 scope
 * (Studio and Performer treated symmetrically).
 *
 * Covers both providers (2026-09-12, see PROGRESS.md's "Entity panel:
 * cover ThePornDB releases too" section) -- this panel used to resolve
 * only the entity's *StashDB* stash_id, so a ThePornDB-sourced release
 * for the same real-world entity (filed under ThePornDB's own,
 * different stash_id) never showed up here, even though the main
 * Releases page's List/Calendar views already covered it. Found live
 * 2026-09-12: a ThePornDB-only release for "Private Stars" appeared on
 * the Calendar but not on that studio's own detail page. Now resolves
 * both ids (if present) and queries/merges releases for each.
 */
(function () {
  'use strict';

  const STASHDB_ENDPOINT = 'https://stashdb.org/graphql';
  // Same stash-box-shim endpoint local Stash is configured against for
  // ThePornDB identification -- see stash_client.py's theporndb_endpoint
  // default, kept in sync with that constant.
  const THEPORNDB_ENDPOINT = 'https://theporndb.net/graphql';
  const PANEL_ID = 'srw-entity-panel';
  const BACKLOG_PANEL_ID = 'srw-entity-backlog-panel';
  const SUBSTUDIO_PANEL_ID = 'srw-entity-substudio-panel';

  async function fetchEntityIds(entityType, localId) {
    const isStudio = entityType === 'studio';
    const query = isStudio
      ? `query($id: ID!) { findStudio(id: $id) { id name stash_ids { endpoint stash_id } } }`
      : `query($id: ID!) { findPerformer(id: $id) { id name stash_ids { endpoint stash_id } } }`;
    const response = await fetch('/graphql', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, variables: { id: localId } }),
    });
    const result = await response.json();
    const entity = isStudio ? result?.data?.findStudio : result?.data?.findPerformer;
    if (!entity) return null;
    const stashIds = entity.stash_ids || [];
    const ids = [];
    const stashdbId = stashIds.find((s) => s.endpoint === STASHDB_ENDPOINT)?.stash_id;
    const theporndbId = stashIds.find((s) => s.endpoint === THEPORNDB_ENDPOINT)?.stash_id;
    if (stashdbId) ids.push({ provider: 'stashdb', stashId: stashdbId });
    if (theporndbId) ids.push({ provider: 'theporndb', stashId: theporndbId });
    // Added stash-boxes (FansDB, JAVStash, ...): matched by the endpoint URL registered under Settings > Metadata providers.
    const norm = (u) => String(u || '').trim().replace(/\/+$/, '').toLowerCase();
    await window.SRW.loadProviders();
    for (const p of window.SRW.providerKeys().map((k) => window.SRW.providerInfo(k))) {
      if (p.builtin || !p.url) continue;
      const hit = stashIds.find((s) => norm(s.endpoint) === norm(p.url));
      if (hit) ids.push({ provider: p.key, stashId: hit.stash_id });
    }
    return ids.length ? { ids, name: entity.name } : null;
  }

  // Centralized in core.js (2026-09-21, Metadata Provider
  // generalization) -- see releases.js's identical alias for why.
  const TAB_ID = 'srw-releases-tab';
  const PANE_ID = 'srw-releases-pane';
  const TAB_HASH = 'srm-releases';
  const sceneUrl = window.SRW.sceneUrl;

  function buildPanel(entityType, ids, entityName) {
    const { el } = window.SRW;
    // <details>/<summary> -- collapsed by default, no JS needed for the
    // expand/collapse itself. Keeps the injected section compact on the
    // Studio/Performer page rather than always showing the full list
    // inline, which got noisy once the watchlist grew from ~40 hand-picked
    // entities to 1250+ via the StashDB.org favorites import.
    const icon = window.SRW.entityIcon(entityType);
    const summaryText = el('span', { textContent: 'New releases' });
    const summary = el('summary', {}, [icon, summaryText]);
    const panel = el('details', { id: PANEL_ID, className: 'srw-panel' }, [
      summary,
      el('div', { className: 'srw-panel-body', textContent: 'Loading...' }),
    ]);
    // Remembered per browser across all Studio/Performer pages
    // (2026-09-25) -- one shared key, it's a "do I want this panel
    // open" preference, not a per-entity one.
    window.SRW.rememberOpen(panel, 'entity:new-releases', panel.open);
    refreshPanel(panel, entityType, ids, entityName);
    return panel;
  }

  // Filtered + paged server-side since 2026-09-26 (user principle: paging
  // wherever a list can grow, always with our filter bar). One query for
  // all linked provider ids at once (`entities`).
  async function refreshPanel(panel, entityType, ids, entityName) {
    const body = panel.querySelector('.srw-panel-body');
    const summaryText = panel.querySelector('summary span');
    const entities = ids.map(({ stashId }) => [entityType, stashId]);
    let view = null;
    view = window.SRW.buildFilteredPagedList({
      filters: ['search', 'description', 'vr', 'dates', 'providers'],
      perPage: 25,
      showFilterBarFrom: 10,
      emptyText: 'No new releases found.',
      fetchPage: async (f, page, perPage) => {
        const response = await window.SRW.runOp('releases_query', {
          ...window.SRW.releaseQueryArgs(f), statuses: ['new'], entities, page, per_page: perPage,
        });
        if (response.error) return response;
        return { total: response.total, items: response.rows };
      },
      renderItems: (items) => buildReleaseList(items, () => view.reload()),
      onTotal: (total, active) => {
        // The "Releases" tab on the page shows how many new releases there are (unfiltered numbers only).
        document.querySelectorAll('.srw-entity-tab-count').forEach((n) => { n.textContent = String(total); n.hidden = !total || active; });
        summaryText.textContent = total || active
          ? `New releases — ${total}${active ? ' (filtered)' : ''}`
          : 'New releases — none';
      },
    });
    body.textContent = '';
    body.appendChild(view.node);
    await view.reset();
  }

  // The scene's own preview image, linked to the scene page -- centralized
  // in core.js (2026-09-21), see its own comment.
  const buildReleaseThumb = window.SRW.buildReleaseThumb;

  // `onDone` is called after a successful hand-off/dismiss so the caller
  // can refresh whatever list it's showing -- the "New releases" panel
  // re-queries status='new', the Backlog panel (added 2026-09-13) status=
  // 'backlog', same list-building/action code either way. Every field
  // this needs (provider, entity_type, entity_stashdb_id, scene_stashdb_id)
  // already lives on the release row itself, so neither function needs
  // entityType/ids passed in separately any more.
  function buildReleaseList(releases, onDone, countHint) {
    const list = buildReleaseListItems(releases, onDone);
    // Fire-and-forget: patches an "In Library"/Whisparr-quality badge
    // into each row once the check comes back, rather than delaying
    // the whole list on it -- best-effort, see annotateLibraryStatus().
    annotateLibraryStatus(list, releases, countHint, onDone);
    return list;
  }

  // Just the <ul>/<li> building, no annotateLibraryStatus() call --
  // split out of buildReleaseList() (2026-09-15) so
  // buildGroupedReleaseList() below can build several <ul>s (one per
  // year/quarter group) and still only make ONE batched
  // releases_library_status call across the whole thing afterward,
  // rather than one per group.
  function buildReleaseListItems(releases, onDone, showSearchStatus) {
    const { el } = window.SRW;
    const list = el('ul', { className: 'srw-release-list' });
    releases.forEach((r) => {
      const isTpdb = r.provider !== 'stashdb'; // any provider but StashDB: handled by this sidecar without Whisparr
      const items = [
        buildReleaseThumb(r),
        el('a', {
          className: 'srw-release-title', href: sceneUrl(r.provider, r.scene_stashdb_id),
          target: '_blank', rel: 'noopener noreferrer', textContent: r.title || '(untitled)',
        }),
        // Scene's own studio (2026-09-20, user request) -- most useful
        // on a Performer's own page, where releases span many different
        // studios and this was previously not shown at all. Skipped on
        // a Studio's own page (r.entity_type === 'studio') since it'd
        // just repeat the studio you're already looking at. Plain text,
        // not a link -- no local studio id is available on a performer-
        // matched row (release_repo.py stores only the studio's name,
        // not its id).
        window.SRW.buildStudioContext(r),
        el('span', { className: 'srw-release-date', textContent: r.scene_date || r.scene_created.slice(0, 10) }),
        window.SRW.buildLateAdditionBadge(r),
        window.SRW.buildPairingBadge(r),
      ];
      // "Search visibility" (2026-09-19, user's own request, extended
      // here from the Releases page's own "Sent to Whisparr" list --
      // "die Missing from Catalog Einträge, da sollte die Visibilität
      // auch angezeigt werden"). Only shown for THIS list (`backlog`),
      // not the plain "New releases" one -- a scene that's never been
      // sent anywhere always reads "not searched yet" there, which is
      // true but pure noise on a list that can hold hundreds of
      // entries; "Missing from Catalog" is exactly where the user
      // wants to know whether a past hand-off's search actually found
      // anything.
      if (showSearchStatus) {
        items.push(el('span', {
          className: 'srw-search-status',
          title: r.last_search_at ? `Last checked: ${r.last_search_at.slice(0, 16).replace('T', ' ')}` : '',
          textContent: window.SRW.searchStatusLabel(r.download_job_id, r.last_search_result),
        }));
      }
      // Searched but nothing grabbed (2026-09-25, stage 3) -- same
      // backend-aware manual actions as the Releases page (Whisparr
      // fallback / own-query search), previously only Search now +
      // Dismiss here.
      const searchedWithoutGrab = showSearchStatus && r.status === 'sent_to_whisparr'
        && !r.download_job_id && r.last_search_result;
      const searchActions = searchedWithoutGrab ? window.SRW.buildSearchActions(r, onDone) : null;
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
        // Default to "Add to Wishlist" (primary blue) -- annotateLibraryStatus()
        // below patches this to "Search now" (teal) once the batched
        // Whisparr check comes back and finds this scene already
        // tracked there without a file, or removes it entirely in
        // favor of a badge if it already has one (2026-09-13, see that
        // function and routers/releases.py's library_status()). Labels
        // renamed from "Send to Whisparr"/"Search in Whisparr"
        // (2026-09-19, Milestone 3) -- the actual indexer search now
        // runs in this sidecar, not Whisparr, so "Send to Whisparr"
        // stopped describing what the button does; Whisparr is still
        // registered with for bookkeeping, but that's an implementation
        // detail, not the user-facing action.
        const addOnly = el('button', {
          className: 'srw-btn srw-whisparr-add-only', textContent: 'Add',
          title: 'Put it on the wishlist without searching now -- the scheduled search finds it later',
        });
        addOnly.addEventListener('click', () => window.SRW.confirmInline(addOnly,
          `Add "${r.title || r.scene_stashdb_id}" to your wishlist? It is searched by the schedule, not right now.`,
          () => doHandoff(r, onDone, showSearchStatus, false)));
        items.push(addOnly);
        items.push(el('button', {
          className: 'srw-btn srw-btn-primary srw-whisparr-action',
          textContent: 'Add + search',
          title: 'Put it on the wishlist and search the indexers right now',
          onClick: (e) => confirmHandoff(r, onDone, e.currentTarget.textContent, showSearchStatus, e.currentTarget),
        }));
      }
      if (searchActions) items.push(searchActions);
      items.push(el('button', {
        className: 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss',
        textContent: 'Dismiss',
        onClick: () => dismissRelease(r, onDone),
      }));
      // Same amber accent as the Releases page's Calendar/List views
      // for a ThePornDB-sourced release (2026-09-12).
      const itemClass = `srw-release-item srw-release-item-${r.provider}`; // tint from the provider registry colour
      const li = el('li', { className: itemClass }, items);
      if (window.SRW.isLateAdditionNew(r)) li.classList.add('srw-late-new');
      li.dataset.sceneId = r.scene_stashdb_id;
      li.dataset.sceneDate = r.scene_date || '';
      window.SRW.arrangeReleaseRow(li, r);
      list.appendChild(li);
    });
    return list;
  }

  // Batched releases_library_status call, extracted (2026-09-15) so
  // buildGroupedReleaseList()/refreshBacklogResults() below can AWAIT
  // it *before* building any DOM -- unlike annotateLibraryStatus()'s
  // fire-and-forget "patch it in afterward" pattern (fine for a flat
  // list where a late-arriving badge is just a small visual update),
  // grouping needs to know which scenes are satisfied *before*
  // deciding which year/quarter groups even exist, or empty groups
  // and stale counts show up first and then change underneath the
  // user (2026-09-15, user: "das mit den falschen Zahlen und den
  // Gruppen ohne Einträge gefällt mir nicht"). Returns {} (not null)
  // on any error or an empty input, same best-effort spirit as
  // before -- every caller already treats "no entry for this id" as
  // "nothing known", so an empty map is a safe, valid result to hand
  // back rather than a special case every caller has to check for.
  async function fetchLibraryStatus(releases) {
    const scenes = releases.filter((r) => r.provider !== 'theporndb')
      .map((r) => ({ scene_stashdb_id: r.scene_stashdb_id, is_vr: !!r.is_vr }));
    if (!scenes.length) return {};
    try {
      const status = await window.SRW.runOp('releases_library_status', { scenes });
      return status.error ? {} : status;
    } catch (e) {
      return {};
    }
  }

  // Applies one scene's library_status() result to its already-built
  // <li> -- shared by annotateLibraryStatus() (fire-and-forget, "New
  // releases") and buildGroupedReleaseList() (status already known
  // before building, "Missing from Catalog") so the actual decision
  // logic isn't duplicated between the two. Returns true if the row
  // was satisfied (and thus removed) so callers can adjust their own
  // counts/messaging.
  function decorateReleaseRow(li, info, release, onDone) {
    if (!info) return false;
    if (info.satisfied) {
      li.remove();
      return true;
    }
    const { el } = window.SRW;
    const actionBtn = li.querySelector('.srw-whisparr-action');
    if (info.whisparr_exists || info.whisparr_has_file || info.whisparr_file_mismatch) li.querySelectorAll('.srw-whisparr-add-only').forEach((x) => x.remove());
    if (info.in_library) {
      const label = info.quality ? `In Library — ${info.quality}` : 'In Library';
      const badge = el('span', { className: 'srw-release-in-library', textContent: label });
      window.SRW.rowMeta(li).appendChild(badge);
      window.SRW.addOpenSceneLink(li, (release && release.provider) || 'stashdb', (release && release.scene_stashdb_id) || li.dataset.sceneId);
    }
    // Whichever of these is true, it's decided from the same batched
    // check the caller already ran -- see routers/releases.py's
    // library_status() docstring for why this needs a live Whisparr
    // lookup (Stash's own data can't answer "does Whisparr already
    // track this"). Mismatch is checked first -- it's a stricter
    // version of "has a file" (Whisparr's own hasFile is true in
    // both), so it must win before the plain has_file branch below
    // ever runs.
    if (info.whisparr_file_mismatch) {
      // 2026-09-14: Whisparr's file doesn't actually match this scene
      // once cross-checked against Stash's own scan -- Whisparr itself
      // has no way to know this (it never fingerprint-verifies against
      // StashDB), so its "hasFile" status is misleading here. Rebinds
      // the button to a fresh node (cloneNode strips confirmHandoff's
      // own listener -- addEventListener alone would leave both firing
      // on click) wired to confirmFixMismatch() instead.
      window.SRW.rowMeta(li).appendChild(window.SRW.mismatchWarning(info));
      if (actionBtn && release) {
        const fixBtn = actionBtn.cloneNode(false);
        fixBtn.textContent = 'Fix wrong download';
        fixBtn.className = 'srw-btn srw-btn-danger';
        fixBtn.addEventListener('click', () => confirmFixMismatch(release, onDone, fixBtn));
        actionBtn.replaceWith(fixBtn);
      }
    } else if (info.whisparr_has_file) {
      // Nothing left to send or search for -- Whisparr already has a
      // file, Stash just hasn't picked it up yet (see
      // .srw-release-whisparr-file's CSS comment). Replaces the button
      // entirely rather than leaving a pointless action.
      if (actionBtn) actionBtn.remove();
      window.SRW.rowMeta(li).appendChild(el('span', {
        className: 'srw-release-whisparr-file',
        textContent: info.in_library ? 'Whisparr holds this file too -- the library copy is below the quality profile\'s cutoff'
          : 'Downloaded (Whisparr) — awaiting Stash rescan',
      }));
    } else if (info.whisparr_exists && actionBtn) {
      actionBtn.textContent = 'Search now';
      actionBtn.classList.remove('srw-btn-primary');
      actionBtn.classList.add('srw-btn-whisparr');
    }
    return false;
  }

  // One plain list for a page of "Missing from Catalog" releases (already sorted newest first by the caller; the old
  // Year > Quarter folding was dropped in v0.101.3 now that the list pages), each row decorated with its library/search status.
  function buildFlatReleaseList(releases, onDone, statusById) {
    const ul = buildReleaseListItems(releases, onDone, true);
    releases.forEach((r) => {
      const li = ul.querySelector(`li[data-scene-id="${CSS.escape(r.scene_stashdb_id)}"]`);
      if (li) decorateReleaseRow(li, statusById[r.scene_stashdb_id], r, onDone);
    });
    return ul;
  }

  // Bulk "Add/Search all" (2026-09-15, user request; label updated
  // 2026-09-19 -- see confirmHandoff()'s own rename note) -- one button
  // that just calls the same releases_handoff op, once per eligible
  // release, sequentially (not in parallel -- this project's own
  // established caution against hammering Whisparr with concurrent
  // requests, see stash_client.py's _bulk_favorite() docstring for the
  // same reasoning applied there). `eligible` is expected pre-filtered
  // by the caller (see refreshBacklogResults()) to StashDB-only,
  // not-mismatched, not-already-downloaded releases -- this function
  // doesn't re-check any of that itself.
  function buildBulkHandoffButton(eligible, onDone) {
    if (!eligible.length) return null;
    const { el } = window.SRW;
    const btn = el('button', {
      // Normal primary blue (v0.101.2): teal means "Search now" for a release that is already on the wishlist; this bulk
      // button covers both adding and searching, so it carries no special meaning.
      className: 'srw-btn srw-btn-primary srw-bulk-handoff-btn',
      textContent: `Add/Search all (${eligible.length})`,
    });
    btn.addEventListener('click', () => runBulkHandoff(eligible, btn, onDone));
    return btn;
  }

  function runBulkHandoff(eligible, btn, onDone) {
    window.SRW.confirmInline(btn,
      `Add/search ${eligible.length} scene(s)? Each one not already tracked yet gets added to your `
      + `wishlist and searched; each one already tracked (with no file yet) gets a fresh search instead. `
      + `This can take a while for a lot of scenes, since they're sent one at a time.`,
      async () => {
        // confirmInline hides `btn` on Yes (its own default, meant for
        // a plain one-shot action) -- undone immediately here since
        // this button IS the progress indicator for the loop below
        // (its own textContent), unlike every other confirmInline call
        // site in this plugin.
        btn.style.display = '';
        btn.disabled = true;
        let succeeded = 0;
        const failures = [];
        for (let i = 0; i < eligible.length; i++) {
          const r = eligible[i];
          btn.textContent = `Sending... (${i + 1}/${eligible.length})`;
          try {
            const result = await window.SRW.runOp('releases_handoff', {
              provider: r.provider, entity_type: r.entity_type,
              entity_stashdb_id: r.entity_stashdb_id, scene_stashdb_id: r.scene_stashdb_id,
            });
            if (result.error) failures.push(`${r.title || r.scene_stashdb_id}: ${result.error}`);
            else succeeded += 1;
          } catch (e) {
            failures.push(`${r.title || r.scene_stashdb_id}: ${e.message}`);
          }
        }
        if (succeeded) window.SRW.notify(`Added ${succeeded} scene(s) to the wishlist`, '', 'success');
        if (failures.length) {
          window.SRW.notify(`${failures.length} of ${eligible.length} scene(s) failed`, failures.join('\n'), 'error');
        }
        await onDone();
      },
    );
  }

  // 2026-09-13: user found a backlog scene ("Mean Girls", Academy POV
  // 2018) that was already in their Stash library -- nothing in the UI
  // said so, inviting a pointless duplicate hand-off. One batched
  // request covers every StashDB-sourced release currently in the list
  // (see routers/releases.py's library_status()) -- Stash-only for the
  // "in library"/quality answer itself (once a scene is actually in
  // the library, Stash's own file metadata already has it, the user's
  // own point) plus a small Whisparr quality-profile lookup (not a
  // per-scene Whisparr call) to know whether that quality already
  // `satisfied` the configured profile's cutoff. A satisfied scene is
  // removed from the list entirely (2026-09-13, user: don't even show
  // it once there's nothing left to do); one that's merely in_library
  // but below the target quality still gets an "In Library — Xp" badge
  // as before.
  async function annotateLibraryStatus(list, releases, countHint, onDone) {
    const status = await fetchLibraryStatus(releases);
    let removed = 0;
    Object.entries(status).forEach(([sceneId, info]) => {
      const li = list.querySelector(`li[data-scene-id="${CSS.escape(sceneId)}"]`);
      if (!li) return;
      const release = releases.find((r) => r.scene_stashdb_id === sceneId);
      if (decorateReleaseRow(li, info, release, onDone)) removed += 1;
    });
    window.SRW.resolveSceneLinks(list);
    // The initial "N scene(s) loaded" hint gets stale the moment any
    // row is removed above -- update it here rather than leaving a
    // count that no longer matches what's actually still shown
    // (2026-09-13, caught live: a fully-satisfied load left "4
    // scene(s) loaded" standing over an empty list).
    if (countHint && removed > 0) {
      const remaining = releases.length - removed;
      countHint.textContent = remaining > 0
        ? `${remaining} of ${releases.length} scene(s) need attention.`
        : `All ${releases.length} loaded scene(s) are already satisfied.`;
    }
  }

  async function confirmHandoff(release, onDone, actionLabel, localPatch, btn) {
    // Confirm-first per PROGRESS.md's 2026-09-11 decision, for the
    // first-time "Add to Wishlist" only -- "Search now" (a release
    // already added once before) fires immediately without a dialog
    // (2026-09-19, user's explicit request: a confirm() here also
    // turned out to silently no-op if the browser had suppressed
    // further dialogs on the page after an earlier one got stuck, e.g.
    // from an automated click -- worth avoiding that whole class of
    // problem for an action this low-stakes, since it's re-searching
    // something already on the wishlist, not adding it fresh). Now a
    // non-blocking inline prompt either way (2026-09-20, see core.js's
    // confirmInline()), so this concern is moot for future call sites,
    // but "Search now" still skips it outright -- no reason to ask
    // twice for a re-search.
    if (actionLabel !== 'Search now') {
      const title = release.title || release.scene_stashdb_id;
      window.SRW.confirmInline(btn, `Add "${title}" to your wishlist and search for it?`,
        () => doHandoff(release, onDone, localPatch));
      return;
    }
    await doHandoff(release, onDone, localPatch);
  }

  async function doHandoff(release, onDone, localPatch, search = true) {
    try {
      const result = await window.SRW.runOp('releases_handoff', {
        provider: release.provider, entity_type: release.entity_type,
        entity_stashdb_id: release.entity_stashdb_id, scene_stashdb_id: release.scene_stashdb_id,
        ...(search ? {} : { search: false }),
      });
      if (result.error) {
        window.SRW.notify(`Adding failed: ${release.title || ''}`, result.error, 'error');
        return;
      }
      window.SRW.notifyHandoff(release.title, result);
      // `localPatch` (2026-09-19, v0.41.2, user report: a full
      // onDone() reload here collapsed all the "Missing from Catalog"
      // year/quarter groups back to closed just to show an updated
      // status label) -- update this one row's search-status text and
      // library/Whisparr badges in place instead of rebuilding the
      // whole tree. Only used for "Missing from Catalog" (which keeps
      // a handed-off release visible either way, see
      // refreshBacklogResults()'s own status-query note) -- the plain
      // "New releases" list still needs the full onDone() reload,
      // since a handed-off release genuinely should disappear from a
      // status='new'-only query on its next fetch.
      if (localPatch) await patchReleaseRow(release, result, onDone);
      else await onDone();
    } catch (e) {
      window.SRW.notify(`Adding failed: ${release.title || ''}`, e.message, 'error');
    }
  }

  // See confirmHandoff()'s `localPatch` note above. Best-effort, same
  // spirit as annotateLibraryStatus() -- a failed patch just leaves the
  // row showing its pre-search state until the next real refresh,
  // nothing breaks.
  async function patchReleaseRow(release, result, onDone) {
    const li = document.querySelector(`li[data-scene-id="${CSS.escape(release.scene_stashdb_id)}"]`);
    if (!li) return;
    const statusSpan = li.querySelector('.srw-search-status');
    if (statusSpan) {
      statusSpan.textContent = result.search_skipped ? 'on the wishlist, searched by the schedule'
        : (result.search_running ? 'searching in the background...' : window.SRW.searchStatusLabel(null, result.search_result));
    }
    li.querySelectorAll('.srw-whisparr-add-only').forEach((x) => x.remove()); // it is on the wishlist now
    try {
      const status = await window.SRW.runOp('releases_library_status', {
        scenes: [{ scene_stashdb_id: release.scene_stashdb_id, is_vr: !!release.is_vr }],
      });
      if (!status.error) {
        decorateReleaseRow(li, status[release.scene_stashdb_id], release, onDone);
      }
    } catch (e) { /* best-effort */ }
  }

  async function confirmFixMismatch(release, onDone, btn) {
    // Real destructive action (deletes a file from disk, see
    // handoff.py's fix_mismatched_download()) -- deliberately its own,
    // more explicit confirm text rather than reusing confirmHandoff()'s,
    // since "search and download" undersells what this actually does.
    const title = release.title || release.scene_stashdb_id;
    window.SRW.confirmInline(btn,
      `Whisparr's file for "${title}" doesn't match this scene -- delete the wrong file from Whisparr `
      + `(and disk) and have it search for a correct release instead?`,
      async () => {
        try {
          const result = await window.SRW.runOp('releases_fix_mismatch', {
            provider: release.provider, entity_type: release.entity_type,
            entity_stashdb_id: release.entity_stashdb_id, scene_stashdb_id: release.scene_stashdb_id,
          });
          if (result.error) {
            window.SRW.notify('Fix wrong download', `Fix failed: ${result.error}`, 'error');
            return;
          }
          await onDone();
        } catch (e) {
          window.SRW.notify('Fix wrong download', `Fix failed: ${e.message}`, 'error');
        }
      },
      { danger: true },
    );
  }

  async function dismissRelease(release, onDone) {
    try {
      // Scene-scoped, not entity-scoped (see routers/releases.py's
      // dismiss() docstring) -- dismissing here also clears this same
      // scene if it's also showing under another watched entity
      // elsewhere, e.g. the main Releases page's merged view.
      const res = await window.SRW.runOp('releases_dismiss', {
        provider: release.provider, scene_stashdb_id: release.scene_stashdb_id,
      });
      window.SRW.releaseDismissedNotice(release, res, onDone);
      await onDone();
    } catch (e) {
      window.SRW.notify('Dismiss', `Dismiss failed: ${e.message}`, 'error');
    }
  }

  const BACKLOG_TITLE_DEFAULT = 'Missing from Catalog';

  // Backlog panel (2026-09-13, see PROGRESS.md's "On-demand backlog
  // browsing, bypassing Whisparr's Monitored flag" section) -- StashDB
  // only, since hand-off itself only works for StashDB-sourced scenes
  // and ThePornDB's REST API has no comparable date-range filter to
  // build the year picker from. `localId` is this Stash instance's own
  // numeric id for the entity (route.id from injectPanel) -- needed for
  // the completeness check below, which filters local scenes by it.
  // Sub-studio checklist (2026-10-03, v0.93.0): a watched network/parent studio also watches its sub-studios (api/stashdb_client.py
  // _entity_filters). Un-ticking one hides it (its releases that came in through a studio leave the Calendar/List; performer hits
  // stay) -- the same list as "Hide studio" on a release row and Settings > Hidden studios. Loaded when the panel is opened.
  function buildSubStudiosPanel(ids, entityName) {
    const { el } = window.SRW;
    const stashdbEntry = ids.find((i) => i.provider === 'stashdb');
    if (!stashdbEntry) return null;
    const summaryText = el('span', { textContent: 'Sub-studios' });
    const body = el('div', { className: 'srw-panel-body', textContent: 'Loading...' });
    const panel = el('details', { id: SUBSTUDIO_PANEL_ID, className: 'srw-panel' }, [
      el('summary', {}, [window.SRW.entityIcon('studio'), summaryText]), body]);
    window.SRW.rememberOpen(panel, 'entity:sub-studios', false);
    let loaded = false;
    panel.addEventListener('toggle', async () => {
      if (!panel.open || loaded) return;
      loaded = true;
      const r = await window.SRW.runOp('muted_studios_children', { studio_id: stashdbEntry.stashId });
      if (r.error) { loaded = false; body.textContent = `Could not load the sub-studios: ${r.error}`; return; }
      const studios = r.studios || [];
      if (!studios.length) { body.textContent = `${entityName || 'This studio'} has no sub-studios at StashDB.`; summaryText.textContent = 'Sub-studios — none'; return; }
      const list = el('div', { style: 'max-height: 26rem; overflow: auto' });
      const filterInput = el('input', { type: 'search', placeholder: `Filter ${studios.length} sub-studios…`, className: 'srw-search-input' });
      const count = el('span', { className: 'srw-hint' });
      // Entries with releases first (most releases on top), the rest alphabetically -- a network like Anal Vids has ~190 channels, few with releases.
      studios.sort((a, b) => (b.releases - a.releases) || a.name.localeCompare(b.name));
      const withReleases = studios.filter((x) => x.releases).length;
      const onlyBox = el('input', { type: 'checkbox' });
      onlyBox.checked = withReleases > 0;
      const onlyLabel = el('label', { className: 'srw-provider-filter-item', title: 'Hide the sub-studios without any release of ours (hidden ones always stay in the list)' },
        [onlyBox, ` only with releases (${withReleases})`]);
      const rows = studios.map((st) => {
        const cb = el('input', { type: 'checkbox' });
        cb.checked = !st.muted;
        cb.addEventListener('change', async () => {
          cb.disabled = true;
          const res = cb.checked
            ? await window.SRW.runOp('muted_studios_unmute', { studio_id: st.id })
            : await window.SRW.runOp('muted_studios_mute', { studio_id: st.id, name: st.name, provider: 'stashdb', parent_id: st.parent_id });
          cb.disabled = false;
          if (res.error) { cb.checked = !cb.checked; window.SRW.notify('Sub-studios', res.error, 'error'); return; }
          st.muted = !cb.checked;
          window.SRW.notify('Sub-studios', cb.checked ? `“${st.name}” is shown again (${res.restored} release(s) restored).`
            : `“${st.name}” hidden (${res.hidden} release(s)).`, 'success');
          updateCount();
        });
        const row = el('label', { className: 'srw-provider-filter-item', style: 'display: block; padding: 2px 0' }, [
          cb, ` ${st.name}`, el('span', { className: 'srw-hint', textContent: st.releases ? `  ·  ${st.releases} release(s)` : '' })]);
        return { st, row };
      });
      const updateCount = () => {
        const hidden = studios.filter((x) => x.muted).length;
        count.textContent = ` ${studios.length - hidden} of ${studios.length} shown${hidden ? `, ${hidden} hidden` : ''}`;
        summaryText.textContent = `Sub-studios — ${studios.length}${hidden ? ` (${hidden} hidden)` : ''}`;
      };
      const applyFilter = () => {
        const q = filterInput.value.trim().toLowerCase();
        list.replaceChildren(...rows
          .filter((x) => (!q || x.st.name.toLowerCase().includes(q)) && (!onlyBox.checked || x.st.releases || x.st.muted))
          .map((x) => x.row));
      };
      filterInput.addEventListener('input', applyFilter);
      onlyBox.addEventListener('change', applyFilter);
      body.replaceChildren(
        el('p', { className: 'srw-hint', textContent: 'Ticked = its releases show up in the Calendar and List (as long as this studio is watched). Un-tick the sub-studios you do not want: their releases that came in through a studio are hidden, new ones too; releases found through a favourite performer are not affected.' }),
        el('div', { className: 'srw-toolbar' }, [filterInput, onlyLabel, count]), list);
      updateCount(); applyFilter();
    });
    return panel;
  }

  function buildBacklogPanel(entityType, entry, entityName, localId) {
    const { el } = window.SRW;
    if (!entry || entry.provider === 'theporndb') return null; // ThePornDB has no stash-box year catalog
    const provider = entry.provider;

    const icon = window.SRW.entityIcon(entityType);
    const summaryText = el('span', { textContent: `${BACKLOG_TITLE_DEFAULT}${provider === 'stashdb' ? '' : ` (${window.SRW.providerLabel(provider)})`}` });
    const summary = el('summary', {}, [icon, summaryText]);
    const body = el('div', { className: 'srw-panel-body', textContent: 'Loading...' });
    const panel = el('details', { id: `${BACKLOG_PANEL_ID}-${provider}`, className: 'srw-panel srw-backlog-panel' }, [summary, body]);
    // Remembered per browser (2026-09-25), one shared key like the
    // "New releases" panel. Restoring it open fires `toggle`, so the
    // lazy loader below still runs.
    window.SRW.rememberOpen(panel, 'entity:missing-from-catalog', false);

    let loaded = false;
    panel.addEventListener('toggle', () => {
      if (panel.open && !loaded) {
        loaded = true;
        initBacklogPanel(panel, summaryText, provider, entityType, entry.stashId, entityName, localId);
      }
    });
    return panel;
  }

  // Cache-first (2026-09-13, user reported the year overview/picker
  // re-running on every single visit felt wasteful) -- expanding the
  // panel only ever shows what's already stored locally (a plain DB
  // read, no StashDB calls at all). The actual live StashDB check (the
  // year-by-year preview, ~12 small requests) now only runs when the
  // user explicitly clicks "Check StashDB for years", not automatically
  // on every expand.
  async function initBacklogPanel(panel, summaryText, provider, entityType, stashId, entityName, localId) {
    const { el } = window.SRW;
    const body = panel.querySelector('.srw-panel-body');
    body.textContent = '';

    const resultsArea = el('div', { className: 'srw-backlog-results' });
    const pickerArea = el('div', { className: 'srw-backlog-picker' });
    // The year overview (which years are loaded / can be loaded) sits ABOVE the scenes, so it is seen first.
    body.appendChild(pickerArea);
    body.appendChild(resultsArea);

    // Both of these are now cache-first (2026-09-13) -- the scene list
    // is always a plain local DB read, and the year overview reuses
    // whatever count_scenes_by_year() result the *backend* already has
    // cached (see routers/backlog.py's years()), so opening the panel
    // costs zero StashDB requests once it's been checked at least once.
    // Only clicking "Refresh" below re-runs the live StashDB check.
    await refreshBacklogResults(resultsArea, provider, entityType, stashId, entityName);
    await loadYearOverview(pickerArea, provider, entityType, stashId, entityName, resultsArea, false);
    await loadCompleteness(summaryText, provider, entityType, stashId, localId);
  }

  // "Missing from Catalog — 152/160" (or "Complete — 160/160" once
  // everything's owned at the desired quality) -- 2026-09-13, see
  // PROGRESS.md's "Missing from Catalog" completeness section for the
  // is_vr_dominant()/cutoff-resolution reasoning behind this number.
  // Best-effort and separate from the results/picker above: a failure
  // here just leaves the plain default title, nothing else breaks.
  async function loadCompleteness(summaryText, provider, entityType, stashId, localId) {
    const providerSuffix = provider === 'stashdb' ? '' : ` (${window.SRW.providerLabel(provider)})`;
    try {
      const result = await window.SRW.runOp('backlog_completeness', {
        provider, entity_type: entityType, entity_stashdb_id: stashId, local_id: localId,
      });
      if (result.error || typeof result.total !== 'number') return;
      const ratio = `${result.in_library}/${result.total}`;
      if (result.complete) {
        summaryText.textContent = `Complete — ${ratio}${providerSuffix}`;
        summaryText.classList.add('srw-backlog-complete');
      } else {
        summaryText.textContent = `${BACKLOG_TITLE_DEFAULT} — ${ratio}${providerSuffix}`;
      }
    } catch (e) {
      // Best-effort -- keep the default title.
    }
  }

  // "Missing from Catalog" must include both a backlog-fetched scene's
  // original status ('backlog') AND 'sent_to_whisparr' -- 2026-09-13,
  // user: "warum zeigt Missing from Catalog Einträge nicht mehr an die
  // ich an Whisparr geschickt habe? müsste die Anzeige nicht auf Search
  // in Whisparr umspringen?" A hand-off flips status away from
  // 'backlog' (see release_repo.set_status_for_scene()), which had
  // silently made the scene vanish from a status='backlog'-only query
  // even though it's still genuinely missing from the local library
  // (no file yet) -- worse, it also made the "Search in Whisparr"
  // re-search action (2026-09-13's own "Send vs Search" feature)
  // unreachable after the very first click, since the row disappeared
  // before a second search could ever be triggered from here. Querying
  // both statuses and letting annotateLibraryStatus()'s existing
  // whisparr_exists/whisparr_has_file check relabel/hide each row is
  // enough -- a scene only drops out for real once import_check.py
  // confirms a file and flips it to 'imported' (not queried here), or
  // it's dismissed. No backend change needed: two small requests
  // scoped to one entity's own list, merged client-side, rather than
  // widening the shared /releases endpoint's single-status filter used
  // by the List/Calendar views elsewhere for their own separate
  // per-status sections.
  //
  // 2026-09-15: also includes 'import_failed' -- import_check.py now
  // sets this (instead of 'imported') when it confirms, via StashDB's
  // own fingerprints, that Whisparr's file for a scene is genuinely
  // the wrong content (see verification.py). Without this, a release
  // the background checker had already caught would silently vanish
  // from this panel the moment it flipped away from
  // 'sent_to_whisparr' -- exactly the same class of bug as the
  // 'sent_to_whisparr' omission above, just for a status that didn't
  // exist yet at the time that first fix was written.
  async function fetchReleasesByStatuses(statuses, provider, entityType, stashId, limit) {
    const results = await Promise.all(statuses.map((status) =>
      window.SRW.runOp('releases_list', { status, provider, entity_type: entityType, entity_stashdb_id: stashId, limit })
    ));
    for (const r of results) {
      if (r && r.error) return r; // surface the first error, same shape callers already expect
    }
    return results.flat();
  }

  async function loadYearOverview(pickerArea, provider, entityType, stashId, entityName, resultsArea, refresh) {
    const { el } = window.SRW;
    pickerArea.textContent = refresh ? `Checking ${window.SRW.providerLabel(provider)}...` : 'Loading year overview...';
    try {
      const result = await window.SRW.runOp('backlog_years', {
        provider, entity_type: entityType, entity_stashdb_id: stashId, refresh,
      });
      if (result.error) {
        pickerArea.textContent = `Release Manager: ${result.error}`;
        return;
      }
      const counts = result.years || {};
      const years = Object.keys(counts).map(Number).sort((a, b) => b - a); // newest first
      pickerArea.textContent = '';
      if (!years.length) {
        pickerArea.appendChild(el('p', { className: 'srw-hint', textContent: `No scenes found for this entity on ${window.SRW.providerLabel(provider)}.` }));
        return;
      }
      // Which years already have backlog scenes stored -- a fresh,
      // cheap local DB read (2026-09-13: this used to be scraped off
      // resultsArea's rendered <li>s instead, but annotateLibraryStatus()
      // now removes a fully-satisfied scene's row entirely, which
      // silently made an already-loaded-but-now-fully-satisfied year
      // look "not loaded" again -- caught live by the user). Reading
      // the underlying data instead of the post-hide DOM fixes that;
      // this is a plain local DB query, not a StashDB request, so
      // doesn't add to the "don't waste StashDB calls" concern this
      // whole panel is built around.
      // Explicitly recorded loads since 2026-09-26 (plus older loads still
      // visible through their rows), see routers/backlog.py's years().
      const loadedYears = new Map(Object.entries(result.loaded_years || {}).map(([y, at]) => [Number(y), at]));
      pickerArea.appendChild(buildYearPicker(
        years, counts, loadedYears, provider, entityType, stashId, entityName, resultsArea, pickerArea, result.checked_at,
        result.window || {},
      ));
    } catch (e) {
      pickerArea.textContent = `Release Manager: ${e.message}`;
    }
  }

  // Year picker (2026-09-26 additions): the entity's date window can be
  // edited right here, years outside it are greyed out, loaded years are
  // marked (✓, not preselected), and "All" loads every in-window year not
  // loaded yet -- with a warning when no From date limits it.
  function buildYearPicker(years, counts, loadedYears, provider, entityType, stashId, entityName, resultsArea, pickerArea,
    checkedAt, win) {
    const { el, confirmInline } = window.SRW;
    const fromYear = win.from ? Number(win.from.slice(0, 4)) : null;
    const untilYear = win.until ? Number(win.until.slice(0, 4)) : null;
    const inWindow = (year) => (fromYear === null || year >= fromYear) && (untilYear === null || year <= untilYear);

    const checkboxes = years.map((year) => {
      const cb = el('input', { type: 'checkbox', className: 'srw-backlog-year-checkbox' });
      cb.dataset.year = String(year);
      const loaded = loadedYears.has(year);
      const outside = !inWindow(year);
      cb.disabled = outside;
      const label = el('label', {
        className: `srw-backlog-year-label${loaded ? ' srw-year-loaded' : ''}${outside ? ' srw-year-outside' : ''}`,
        title: outside ? 'Outside this entity\'s date window'
          : (loaded ? `Already loaded${loadedYears.get(year) ? ` (${loadedYears.get(year).slice(0, 10)})` : ''} -- tick to reload` : ''),
      }, [cb, ` ${loaded ? '✓ ' : ''}${year} (${counts[year]})`]);
      return label;
    });
    const yearList = el('div', { className: 'srw-backlog-years' }, checkboxes);

    const loadBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Load selected years' });
    loadBtn.addEventListener('click', () => {
      const selected = checkboxes.map((l) => l.querySelector('input')).filter((cb) => cb.checked)
        .map((cb) => Number(cb.dataset.year));
      loadSelectedYears(selected, loadBtn, resultsArea, pickerArea, provider, entityType, stashId, entityName);
    });

    const allYears = years.filter((y) => inWindow(y) && !loadedYears.has(y));
    const allScenes = allYears.reduce((n, y) => n + (counts[y] || 0), 0);
    const allBtn = el('button', {
      className: 'srw-btn', textContent: `All (${allYears.length})`,
      title: 'Load every year inside the date window that isn\'t loaded yet',
    });
    allBtn.disabled = !allYears.length;
    allBtn.addEventListener('click', (e) => {
      const warning = win.from
        ? `Load ${allYears.length} year(s) (up to ${allScenes} scenes) from ${win.from}${win.until ? ` until ${win.until}` : ''}?`
        : `No From date set -- this loads the ENTIRE catalogue not loaded yet: ${allScenes} scenes in `
          + `${allYears.length} year(s). Set a From date (here or as global default) to limit it. Load anyway?`;
      confirmInline(e.currentTarget, warning, () => {
        allBtn.style.display = '';
        loadSelectedYears(allYears, allBtn, resultsArea, pickerArea, provider, entityType, stashId, entityName);
      }, { danger: !win.from });
    });

    const refreshBtn = window.SRW.iconButton('button', 'faRotate', 'Reload', {
      className: 'srw-btn srw-btn-sm',
      title: `Re-check ${window.SRW.providerLabel(provider)} for new years/scenes instead of using the cached overview`,
      onClick: () => loadYearOverview(pickerArea, provider, entityType, stashId, entityName, resultsArea, true),
    });
    const controls = [loadBtn, allBtn, refreshBtn];
    if (checkedAt) controls.push(el('span', { className: 'srw-hint', textContent: `Checked ${checkedAt} UTC` }));

    return el('div', {}, [
      buildWindowEditor(win, provider, entityType, stashId, entityName, resultsArea, pickerArea),
      yearList,
      el('div', { className: 'srw-backlog-year-controls' }, controls),
    ]);
  }

  function buildWindowEditor(win, provider, entityType, stashId, entityName, resultsArea, pickerArea) {
    const { el, confirmInline } = window.SRW;
    const fromInput = el('input', { type: 'date', className: 'srw-list-date-filter', value: win.own_from || '' });
    const untilInput = el('input', { type: 'date', className: 'srw-list-date-filter', value: win.until || '' });
    const status = el('span', { className: 'srw-key-status' });
    const saveBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Save' });
    saveBtn.addEventListener('click', async () => {
      const result = await window.SRW.runOp('backlog_set_window', {
        provider, entity_type: entityType, entity_stashdb_id: stashId,
        from_date: fromInput.value, until_date: untilInput.value,
      });
      if (result.error) { status.textContent = ` error: ${result.error}`; return; }
      status.textContent = ' saved';
      const reload = () => loadYearOverview(pickerArea, provider, entityType, stashId, entityName, resultsArea, false);
      if (result.open_outside > 0) {
        status.textContent = '';
        const offer = el('button', { className: 'srw-btn srw-btn-sm srw-btn-danger',
          textContent: `Dismiss ${result.open_outside} open release(s) outside the window…` });
        status.appendChild(offer);
        offer.addEventListener('click', (e) => confirmInline(e.currentTarget,
          `Dismiss ${result.open_outside} open release(s) of "${entityName}" released outside the new window?`,
          async () => {
            const done = await window.SRW.runOp('backlog_dismiss_outside_window', {
              provider, entity_type: entityType, entity_stashdb_id: stashId,
            });
            if (done.error) window.SRW.notify('Date window', done.error, 'error');
            else window.SRW.dismissedNotice('Date window', `${done.dismissed} release(s) dismissed.`,
              () => window.SRW.runOp('releases_restore', { rows: done.previous || [] }),
              async () => { await refreshBacklogResults(resultsArea, provider, entityType, stashId, entityName); reload(); });
            await refreshBacklogResults(resultsArea, provider, entityType, stashId, entityName);
            reload();
          }, { danger: true }));
        return;
      }
      reload();
    });
    const defaultHint = win.from_is_default
      ? el('span', { className: 'srw-hint', textContent: ` (From: global default ${win.from})` }) : null;
    return el('div', { className: 'srw-window-editor' }, [
      el('span', { className: 'srw-list-date-range-label', textContent: 'Date window: from' }), fromInput,
      el('span', { className: 'srw-list-date-range-label', textContent: 'until' }), untilInput,
      saveBtn, defaultHint, status,
      el('div', { className: 'srw-hint', textContent: 'Applies to everything for this entity: which new/late scenes '
        + 'the scan stores, which years/scenes load here, and Auto → Wishlist.' }),
    ]);
  }

  async function loadSelectedYears(years, loadBtn, resultsArea, pickerArea, provider, entityType, stashId, entityName) {
    const selectedYears = years.slice().sort((a, b) => a - b);
    const idleLabel = loadBtn.textContent;
    if (!selectedYears.length) {
      window.SRW.notify('Backlog', 'Select at least one year first.', 'info');
      return;
    }

    loadBtn.disabled = true;
    // Sequential, not Promise.all -- each year is its own paginated
    // StashDB fetch (see stashdb_client.py's fetch_scenes_for_year()),
    // and running them one at a time both respects the same rate-limit
    // courtesy the regular scan already uses and lets the button text
    // double as a simple progress indicator, without needing a separate
    // background-job/polling endpoint for what's normally a handful of
    // bounded, single-entity fetches.
    for (const year of selectedYears) {
      loadBtn.textContent = `Fetching ${year}...`;
      try {
        const result = await window.SRW.runOp('backlog_fetch_year', {
          provider, entity_type: entityType, entity_stashdb_id: stashId, year,
        });
        if (result.error) {
          window.SRW.notify('Backlog', `Backlog fetch failed for ${year}: ${result.error}`, 'error');
          loadBtn.disabled = false;
          loadBtn.textContent = idleLabel;
          return;
        }
      } catch (e) {
        window.SRW.notify('Backlog', `Backlog fetch failed for ${year}: ${e.message}`, 'error');
        loadBtn.disabled = false;
        loadBtn.textContent = idleLabel;
        return;
      }
    }
    loadBtn.textContent = idleLabel;
    loadBtn.disabled = false;
    await refreshBacklogResults(resultsArea, provider, entityType, stashId, entityName);
    // Re-render the picker (cache-first, so this costs nothing extra
    // server-side) purely to refresh which years now show "— loaded".
    await loadYearOverview(pickerArea, provider, entityType, stashId, entityName, resultsArea, false);
  }

  // Composite key for a <details> group's open/closed identity, built
  // from its own label plus every ancestor <details>' label (year,
  // then quarter) -- "2022|Q4", not just "Q4", since the bare quarter
  // label repeats once per year and would otherwise open every year's
  // Q4 group at once when restoring. Strips each label's own trailing
  // "— N" count (2026-09-19) since that count can genuinely change
  // between refreshes (a dismissed scene shrinks it) without the
  // group's identity actually changing.
  function detailsGroupKey(details) {
    const parts = [];
    let node = details;
    while (node) {
      const summary = node.querySelector(':scope > summary');
      if (summary) parts.unshift(summary.textContent.replace(/\s*—\s*\d+$/, '').trim());
      node = node.parentElement ? node.parentElement.closest('details') : null;
    }
    return parts.join('|');
  }

  async function refreshBacklogResults(resultsArea, provider, entityType, stashId, entityName) {
    const { el } = window.SRW;
    // Preserve which year/quarter groups were open across this rebuild
    // (2026-09-19, user report: Dismissing/fixing one entry collapsed
    // every other open group back to closed, "was bei mehreren
    // Einträgen nicht optimal ist") -- restored below, right before
    // this function returns.
    const openKeys = new Set();
    resultsArea.querySelectorAll('details[open]').forEach((d) => openKeys.add(detailsGroupKey(d)));
    resultsArea.textContent = 'Loading...';
    try {
      const releases = await fetchReleasesByStatuses(
        ['backlog', 'sent_to_whisparr', 'import_failed'], provider, entityType, stashId, 1000,
      );
      if (releases.error) {
        resultsArea.textContent = `Release Manager: ${releases.error}`;
        return;
      }
      resultsArea.textContent = '';
      if (!releases.length) {
        resultsArea.appendChild(el('p', {
          className: 'srw-hint',
          textContent: 'No backlog loaded yet -- pick some years below.',
        }));
        return;
      }
      const sorted = releases.slice().sort((a, b) => (b.scene_date || '').localeCompare(a.scene_date || ''));
      // Awaited *before* grouping/rendering anything (2026-09-15, see
      // buildGroupedReleaseList()'s own docstring) -- so the year/
      // quarter groups actually built only ever contain scenes that
      // are still missing, with correct counts from the start, rather
      // than a "loaded" tree that then loses rows (and goes stale)
      // once this same check would otherwise run afterward.
      resultsArea.textContent = 'Checking library/Whisparr status...';
      const statusById = await fetchLibraryStatus(sorted);
      const missing = sorted.filter((r) => !(statusById[r.scene_stashdb_id] || {}).satisfied);
      // Scene pairing (v0.182.0): scenes loaded before the cast was recorded get it read now (<= 200 per call, StashDB batches).
      if (missing.some((r) => r.provider === provider && (r.cast_pairing == null || r.cast_names == null))) {
        resultsArea.textContent = `Reading the cast of ${missing.filter((r) => r.cast_pairing == null || r.cast_names == null).length} scene(s)...`;
        let res = null;
        for (let n = 0; n < 50; n += 1) {
          res = await window.SRW.runOp('backlog_pairings', { provider, entity_type: entityType, entity_stashdb_id: stashId });
          if (!res || res.error || !res.remaining) break;
        }
        if (res && !res.error) {
          const again = await fetchReleasesByStatuses(['backlog', 'sent_to_whisparr', 'import_failed'], provider, entityType, stashId, 1000);
          if (!again.error) {
            const castByScene = new Map(again.map((x) => [x.scene_stashdb_id, x]));
            missing.forEach((r) => {
              const x = castByScene.get(r.scene_stashdb_id);
              if (x) { r.cast_pairing = x.cast_pairing; r.cast_names = x.cast_names; }
            });
          }
        }
      }
      resultsArea.textContent = '';
      if (!missing.length) {
        resultsArea.appendChild(el('p', {
          className: 'srw-hint',
          textContent: `All ${sorted.length} loaded scene(s) are already satisfied.`,
        }));
        return;
      }
      const countHint = el('p', {
        className: 'srw-hint',
        textContent: missing.length === sorted.length
          ? `${missing.length} scene(s) need attention.`
          : `${missing.length} of ${sorted.length} scene(s) need attention.`,
      });
      resultsArea.appendChild(countHint);
      // Bulk send/search (2026-09-15, user request) -- everything
      // currently showing "Add to Wishlist" or "Search now"
      // (both go through the same releases_handoff op either way, see
      // handoff.py's own MovieAlreadyExistsError handling -- it
      // already decides add-vs-research per scene on its own).
      // Deliberately excludes whisparr_file_mismatch (needs the
      // separate, destructive "Fix wrong download" confirm per scene,
      // not a bulk one) and whisparr_has_file (nothing to send/search
      // for, it's just awaiting a Stash rescan) -- and ThePornDB
      // releases, which this hand-off has never supported.
      const eligibleForBulk = missing.filter((r) => {
        if (r.provider === 'theporndb') return false;
        const info = statusById[r.scene_stashdb_id];
        return !(info && (info.whisparr_file_mismatch || info.whisparr_has_file));
      });
      const bulkBtn = buildBulkHandoffButton(
        eligibleForBulk, () => refreshBacklogResults(resultsArea, provider, entityType, stashId, entityName),
      );
      if (bulkBtn) resultsArea.appendChild(bulkBtn);
      // Filter + paging (2026-09-26) over the already-loaded rows; each
      // page is still grouped by year/quarter as before. "Added late"
      // narrows to back-catalogue additions found by the scan.
      const view = window.SRW.buildFilteredPagedList({
        filters: ['search', 'description', 'vr', 'late', 'cast', 'pairing', 'dates', 'providers'],
        perPage: 40,
        showFilterBarFrom: 10,
        pairingOptions: window.SRW.pairingOptionsFor(missing),
        castOptions: window.SRW.castOptionsFor(missing, entityType === 'performer' ? entityName : ''),
        fetchPage: window.SRW.localPageFetcher(() => missing, {
          text: (r) => `${r.title || ''} ${r.studio_name || ''}`,
          description: (r) => r.details,
          vr: (r) => r.is_vr,
          provider: (r) => r.provider,
          date: (r) => r.scene_date || r.scene_created,
          late: (r) => r.late_addition,
          pairing: (r) => r.cast_pairing,
          cast: (r) => r.cast_names,
        }),
        // Flat, newest first, one page at a time (v0.101.3) -- the year/quarter folding is gone now that the list pages.
        renderItems: (items) => buildFlatReleaseList(
          items, () => refreshBacklogResults(resultsArea, provider, entityType, stashId, entityName), statusById,
        ),
      });
      resultsArea.appendChild(view.node);
      await view.reset();
      if (openKeys.size) {
        resultsArea.querySelectorAll('details').forEach((d) => {
          if (openKeys.has(detailsGroupKey(d))) d.setAttribute('open', '');
        });
      }
    } catch (e) {
      resultsArea.textContent = `Release Manager: ${e.message}`;
    }
  }

  // injectPanel does several awaits (entity lookup, waitForElement) before
  // it mounts anything. Stash's own React app plus our MutationObserver-
  // based navigation watcher can both call this for the same route in
  // quick succession (init()'s own handleRoute call racing a
  // MutationObserver-detected DOM change) -- confirmed live 2026-09-11:
  // two concurrent calls each found nothing to remove yet (the panel
  // doesn't exist until the first call's awaits resolve), then both went
  // on to mount their own copy, leaving two #srw-entity-panel elements on
  // the page. A generation token makes only the most recently *started*
  // call allowed to actually mount -- any earlier call that resolves late
  // (or the panel-removal step) bails out instead of racing.
  let injectionToken = 0;

  // ==================== Watchlist icon (2026-09-26) ====================
  //
  // An eye icon right next to Stash's own Favorite (heart) button on a
  // Studio/Performer page -- only while the watchlist follows the
  // watchlist TAG (watchlist_source = 'tag', see api/watchlist_source.py);
  // in favorites mode the heart itself already is the watchlist toggle.
  // Clicking adds/removes the tag (removing also drops child tags of it,
  // otherwise a child tag would keep the entity watched). Shown for every
  // Studio/Performer, even one without a StashDB/ThePornDB link yet --
  // tagging it is harmless and it's picked up once it gets one.
  const WATCH_BUTTON_ID = 'srw-watch-button';
  const EYE_ICON = {
    viewBox: '0 0 576 512',
    d: 'M288 32c-80.8 0-145.5 36.8-192.6 80.6C48.6 156 17.3 208 2.5 243.7c-3.3 7.9-3.3 16.7 0 24.6C17.3 304 48.6 356 95.4 399.4 142.5 443.2 207.2 480 288 480s145.5-36.8 192.6-80.6c46.8-43.5 78.1-95.4 93-131.1 3.3-7.9 3.3-16.7 0-24.6-14.9-35.7-46.2-87.7-93-131.1C433.5 68.8 368.8 32 288 32zM144 256a144 144 0 1 1 288 0 144 144 0 1 1 -288 0zm144-64c0 35.3-28.7 64-64 64-7.1 0-13.9-1.2-20.3-3.3-5.5-1.8-11.9 1.6-11.7 7.4 .3 6.9 1.3 13.8 3.2 20.7 13.7 51.2 66.4 81.6 117.6 67.9s81.6-66.4 67.9-117.6c-11.1-41.5-47.8-69.4-88.6-71.1-5.8-.2-9.2 6.1-7.4 11.7 2.1 6.4 3.3 13.2 3.3 20.3z',
  };
  let watchToken = 0;
  let watchObserver = null;

  function buildEyeSvg() {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('viewBox', EYE_ICON.viewBox);
    svg.setAttribute('class', 'svg-inline--fa fa-icon');
    svg.setAttribute('aria-hidden', 'true');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', EYE_ICON.d);
    path.setAttribute('fill', 'currentColor');
    svg.appendChild(path);
    return svg;
  }

  function applyWatchState(btn, state) {
    btn.classList.toggle('srw-watched', !!state.watched);
    btn.classList.toggle('srw-not-watched', !state.watched);
    const tag = state.tag_name || 'Watchlist';
    btn.title = state.watched
      ? (state.direct_tag
        ? `On the watchlist (tag "${tag}") -- click to remove`
        : `On the watchlist via a child tag of "${tag}" -- click to remove (removes those tags too)`)
      : `Not on the watchlist -- click to add (tag "${tag}")`;
    btn.setAttribute('aria-pressed', state.watched ? 'true' : 'false');
  }

  async function injectWatchButton(route) {
    const myToken = ++watchToken;
    watchObserver?.disconnect();
    watchObserver = null;
    document.getElementById(WATCH_BUTTON_ID)?.remove();
    if (route.type !== 'studio' && route.type !== 'performer') return;

    let state;
    try {
      state = await window.SRW.runOp('watchlist_watch_state', { entity_type: route.type, local_id: route.id });
    } catch (e) {
      return; // sidecar unreachable -- no icon rather than a broken one
    }
    if (myToken !== watchToken || !state || state.error || state.mode !== 'tag' || state.found === false) return;

    const heart = await window.SRW.waitForElement('.name-icons .favorite-button', { timeout: 6000 });
    if (myToken !== watchToken || !heart) return;

    const btn = window.SRW.el('button', {
      id: WATCH_BUTTON_ID, type: 'button', className: 'minimal btn btn-primary srw-watch-button',
    });
    btn.appendChild(buildEyeSvg());
    applyWatchState(btn, state);
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      const wanted = !state.watched;
      try {
        const result = await window.SRW.runOp('watchlist_set_watch_state', {
          entity_type: route.type, local_id: route.id, watched: wanted,
        });
        if (result.error) throw new Error(result.error);
        state = result;
        applyWatchState(btn, state);
        const flagged = result.decision?.flagged || [];
        window.SRW.notify(
          wanted ? 'Added to watchlist' : 'Removed from watchlist',
          (wanted ? 'New scenes are picked up from the next scan on.' : 'No longer scanned from the next scan on.')
            + (flagged.length ? ' It still has open releases -- decide on the Releases page whether to keep them.' : ''),
          'success',
        );
      } catch (e) {
        window.SRW.notify('Watchlist', `Couldn't change the watchlist: ${e.message}`, 'error');
      } finally {
        btn.disabled = false;
      }
    });

    document.getElementById(WATCH_BUTTON_ID)?.remove();
    heart.after(btn);
    // Stash's React re-renders the header (e.g. after toggling Favorite
    // or editing the entity) and can drop foreign nodes -- put the icon
    // back next to the (possibly new) heart when that happens.
    const header = heart.closest('.name-icons')?.parentElement || heart.parentElement;
    watchObserver = new MutationObserver(() => {
      if (myToken !== watchToken || document.getElementById(WATCH_BUTTON_ID)) return;
      const currentHeart = document.querySelector('.name-icons .favorite-button');
      if (currentHeart) currentHeart.after(btn);
    });
    watchObserver.observe(header, { childList: true, subtree: true });
  }

  async function injectPanel(route) {
    injectWatchButton(route); // independent of the panel below, which needs a provider link
    const myToken = ++injectionToken;
    document.getElementById(PANEL_ID)?.remove();
    document.querySelectorAll('.srw-backlog-panel').forEach((n) => n.remove());
    document.getElementById(SUBSTUDIO_PANEL_ID)?.remove();
    document.getElementById(TAB_ID)?.remove();
    document.getElementById(PANE_ID)?.remove();
    if (window.SRW_GroupPanel) window.SRW_GroupPanel.clear();
    if (route.type === 'group') { if (window.SRW_GroupPanel && await window.SRW.scopeAllows('movies')) window.SRW_GroupPanel.inject(route); return; }
    if (route.type !== 'studio' && route.type !== 'performer') return;
    if (!(await window.SRW.scopeAllows('scenes'))) return; // Settings > Content: movies only -- the scene releases tab is not offered (the watch button above stays)
    // Must resolve before any release row builds a scene link (sceneUrl()
    // reads this cache synchronously) -- see core.js's own comment. Note
    // this panel today only ever injects for a StashDB/ThePornDB-linked
    // entity (see fetchEntityIds() below) -- a generic-provider-only
    // entity isn't detected yet, a known gap for a later pass.
    await window.SRW.loadMetadataProviderUrls();

    const info = await fetchEntityIds(route.type, route.id);
    if (myToken !== injectionToken) return; // superseded by a newer call
    if (!info) return; // no StashDB/ThePornDB id linked -- nothing to watch for this entity

    // Best-effort mount point: Stash's detail pages render scenes under a
    // '.tab-content' region within the entity's tabbed panel -- confirmed
    // live 2026-09-11 against a real Studio and Performer page. Sequential
    // fallbacks, not a comma-joined selector list -- querySelector on a
    // selector list matches by document order, not priority, which
    // silently broke an earlier version of this same pattern elsewhere in
    // this plugin (see stash-release-manager.js's createPluginRoot()).
    const mount =
      (await window.SRW.waitForElement('.tab-content', { timeout: 4000 })) ||
      document.querySelector('.entity-page') ||
      document.querySelector('.performer-page') ||
      document.querySelector('.studio-page') ||
      document.querySelector('main');
    if (myToken !== injectionToken) return; // superseded by a newer call
    if (!mount) return;

    // Re-check: a superseded call may have mounted since the first removal.
    document.getElementById(TAB_ID)?.remove();
    document.getElementById(PANE_ID)?.remove();
    document.getElementById(PANEL_ID)?.remove();
    document.querySelectorAll('.srw-backlog-panel').forEach((n) => n.remove());
    document.getElementById(SUBSTUDIO_PANEL_ID)?.remove();
    // Studio pages only: the sub-studio checklist sits below the Backlog panel.
    const subPanel = route.type === 'studio' ? buildSubStudiosPanel(info.ids, info.name) : null;
    const backlogPanels = info.ids.map((entry) => buildBacklogPanel(route.type, entry, info.name, route.id)).filter(Boolean);
    // "New releases" first, the deliberately-secondary opt-in Backlog panel below it, the sub-studio checklist last.
    const panel = buildPanel(route.type, info.ids, info.name);
    const tabLink = document.querySelector('a[id^="performer-tabs-tab-"], a[id^="studio-tabs-tab-"]');
    const nav = tabLink && tabLink.closest('.nav-tabs');
    if (nav) {
      mountReleasesTab(nav, mount, [panel, ...backlogPanels, subPanel]);
    } else {
      // Stash's tab strip not found (changed markup?) -- fall back to the old placement above the tabs.
      [subPanel, ...backlogPanels.slice().reverse(), panel].forEach((p) => { if (p) mount.prepend(p); });
    }
  }

  // Own "Releases" tab in Stash's tab strip of Performer/Studio pages (2026-10-04, UI review step 5) instead of several
  // panels above the tabs: unobtrusive, and the panels are only in the way when the user asks for them. Stash has no
  // patch point for the Studio page, so the tab is added to the DOM (works the same on both pages) and shown/hidden here.
  function mountReleasesTab(nav, content, panels, opts = {}) {
    const { el } = window.SRW;
    const tabId = opts.tabId || TAB_ID;
    const paneId = opts.paneId || PANE_ID;
    const tabHash = opts.hash || TAB_HASH;
    const count = el('span', { className: 'srw-entity-tab-count', hidden: true });
    const tab = el('a', { id: tabId, href: `${location.pathname}#${tabHash}`, role: 'tab', className: 'nav-item nav-link' }, [opts.label || 'Releases', count]);
    const pane = el('div', { id: paneId, className: 'srw-entity-tab-pane', hidden: true });
    const real = panels.filter(Boolean);
    // Second level inside the tab: one section at a time (New releases / Missing from Catalog / Sub-studios). The labels
    // mirror each section's own heading, which carries its live numbers ("New releases — 3" -> "New releases (3)").
    const labelOf = (p) => (((p.querySelector('summary') || {}).textContent) || '').trim().replace(/\s+—\s+(.*)$/, ' ($1)');
    real.forEach((p) => { p.open = true; p.classList.add('srw-panel-flat'); });
    if (real.length > 1) {
      pane.appendChild(window.SRW.buildPaneTabs(
        real.map((p) => ({ key: p.id, label: () => labelOf(p), pane: p, watch: p.querySelector('summary') })), 'srw:entity-section'));
    } else {
      real.forEach((p) => { p.hidden = false; });
    }
    real.forEach((p) => pane.appendChild(p));
    nav.appendChild(tab);
    content.appendChild(pane);
    let stashActive = [];
    const stashPanes = () => [...content.children].filter((c) => c !== pane);
    function show() {
      stashActive = [...nav.querySelectorAll('.nav-link.active')].filter((a) => a !== tab);
      nav.querySelectorAll('.nav-link').forEach((a) => { a.classList.remove('active'); a.setAttribute('aria-selected', 'false'); });
      tab.classList.add('active');
      tab.setAttribute('aria-selected', 'true');
      stashPanes().forEach((p) => { p.style.display = 'none'; });
      pane.hidden = false;
      // Full path (Stash's <base href="/">), so a reload opens this tab again.
      try { history.replaceState(null, '', `${location.pathname}${location.search}#${tabHash}`); } catch (e) { /* ignore */ }
    }
    // Leaving the tab takes its hash out of the address, otherwise a reload opens it again (v0.127.3). Full path: Stash's <base href="/">.
    function clearHash() {
      try { if (location.hash === `#${tabHash}`) history.replaceState(null, '', `${location.pathname}${location.search}`); } catch (e) { /* ignore */ }
    }
    function hide() {
      if (pane.hidden) return;
      pane.hidden = true;
      tab.classList.remove('active');
      tab.setAttribute('aria-selected', 'false');
      stashPanes().forEach((p) => { p.style.display = ''; });
      // Stash does not redraw its own tab when the one it already considers active is clicked again.
      stashActive.forEach((a) => { a.classList.add('active'); a.setAttribute('aria-selected', 'true'); });
    }
    tab.addEventListener('click', (e) => { e.preventDefault(); show(); });
    nav.addEventListener('click', (e) => { const l = e.target.closest('.nav-link'); if (l && l !== tab) { clearHash(); hide(); } }, true);
    if (location.hash === `#${tabHash}`) show();
  }

  window.SRW_EntityPanel = { injectPanel, mountTab: mountReleasesTab };
})();
