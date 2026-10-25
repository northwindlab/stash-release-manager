/**
 * StashReleaseManager - Core
 * Shared utilities, settings, route detection, and the runPluginOperation bridge.
 */
(function () {
  'use strict';

  // Must match the directory name this plugin is installed under in Stash
  // (Stash derives a local plugin's id from its folder name, not the .yml
  // manifest -- see stash-sense2's stash-sense-core.js for the exact same
  // rule and the bug it caused when this drifted). Change this if you
  // install under a different folder name.
  // Must equal `version:` in stash-release-manager.yml (scripts/test_plugin_version.py checks it).
  const PLUGIN_VERSION = '0.243.21';
  const PLUGIN_ID = 'stash-release-manager';
  const PLUGIN_NAME = 'StashReleaseManager';

  const DEFAULTS = { sidecarUrl: 'http://localhost:6961' };

  let cachedSettings = null;

  // The only plugin setting is the sidecar's address (the browser needs it before it can ask the sidecar anything). Stash's own
  // URL and API key, and the StashDB / ThePornDB / other stash-box keys, are NOT entered here any more (v0.186.0): the exec script
  // reads the keys from Stash itself and the sidecar keeps them (see stash_release_manager_backend.py's _sync_from_stash()). The
  // address the browser uses for Stash is sent along as a suggestion for the sidecar's Stash URL (taken over only while it has none).
  async function getSettings() {
    if (cachedSettings) return cachedSettings;
    try {
      const response = await fetch('/graphql', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: `query { configuration { plugins } }` }),
      });
      const result = await response.json();
      const pluginConfig = result?.data?.configuration?.plugins?.[PLUGIN_ID];
      cachedSettings = {
        sidecarUrl: (pluginConfig?.sidecarUrl || DEFAULTS.sidecarUrl).replace(/\/$/, ''),
      };
      return cachedSettings;
    } catch (e) {
      console.error(`[${PLUGIN_NAME}] Failed to load settings:`, e);
      return DEFAULTS;
    }
  }

  // Safety check (v0.197.0): moving functions refuse while ANOTHER renaming plugin (renamerOnUpdate ...) is enabled in Stash; the checkbox of the
  // warning ("continue anyway") is remembered for this browser session and sent with the apply operations.
  const RENAMER_APPLY_MODES = new Set(['import_apply', 'organize_apply', 'organize_movies_apply']);
  // The other-renamer warning is only shown while the Organized hook is on -- or after a manual move was held back by it (flag below, this session).
  const RENAMER_BLOCKED_KEY = 'srw:renamer-blocked';
  const renamerBlocked = () => { try { return window.sessionStorage.getItem(RENAMER_BLOCKED_KEY) === '1'; } catch (e) { return false; } };
  const IGNORE_RENAMERS_KEY = 'srw:ignore-renamers';
  function ignoreRenamers() {
    try { return window.sessionStorage.getItem(IGNORE_RENAMERS_KEY) === '1'; } catch (e) { return false; }
  }
  function setIgnoreRenamers(on) {
    try { window.sessionStorage.setItem(IGNORE_RENAMERS_KEY, on ? '1' : '0'); } catch (e) { /* private mode: the warning just stays */ }
  }

  async function runOp(mode, args = {}) {
    const settings = await getSettings();
    const query = `
      mutation RunPluginOperation($plugin_id: ID!, $args: Map!) {
        runPluginOperation(plugin_id: $plugin_id, args: $args)
      }
    `;
    const response = await fetch('/graphql', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        query,
        variables: {
          plugin_id: PLUGIN_ID,
          args: {
            mode, sidecar_url: settings.sidecarUrl,
            stash_url_suggested: window.location.origin,
            ...args,
            ...(RENAMER_APPLY_MODES.has(mode) && ignoreRenamers() ? { ignore_renamers: true } : {}),
          },
        },
      }),
    });
    if (!response.ok) {
      throw new Error(`Plugin operation failed: HTTP ${response.status}`);
    }
    const result = await response.json();
    if (result.errors) {
      throw new Error(result.errors[0]?.message || 'GraphQL error');
    }
    const output = result?.data?.runPluginOperation;
    let value;
    if (typeof output === 'string') {
      try {
        const parsed = JSON.parse(output);
        value = parsed.output ?? parsed;
      } catch {
        value = { error: output };
      }
    } else {
      value = output?.output ?? output ?? {};
    }
    if (RENAMER_APPLY_MODES.has(mode) && value && value.error && Array.isArray(value.renamers)) {
      // held back by another enabled renamer: from now on the safety panels show the warning (with its "continue anyway" box)
      try { window.sessionStorage.setItem(RENAMER_BLOCKED_KEY, '1'); } catch (e) { /* ignore */ }
      window.dispatchEvent(new CustomEvent('srw:renamer-blocked'));
      value = { ...value, error: `${value.error} (The safety check above now shows the details and the "continue anyway" box.)` };
    }
    return value;
  }

  // Centralized here (2026-09-21, Metadata Provider generalization)
  // instead of releases.js/entity-panel.js each keeping their own copy
  // -- those only ever needed to distinguish 'theporndb' from
  // everything-else-is-stashdb, which stopped being true once an
  // arbitrary additional stash-box provider (see
  // api/metadata_provider_repo.py) could show up in a release row's
  // `provider` field. StashDB/ThePornDB stay hardcoded exactly as
  // before (they're not rows in that table, see its own docstring);
  // any other provider's scene URL is derived from its own configured
  // GraphQL endpoint (`.../graphql` stripped, `/scenes/<id>` appended
  // -- the same URL convention every stash-box instance's own web UI
  // uses, confirmed live against FansDB during Stage A's testing).
  // Provider registry (v0.152.0): every provider a release row can come from (StashDB, ThePornDB and the user-added stash-boxes) with its
  // name and colour. Filters, badges, row tints, calendar pills and the legend all read it, so a new provider needs no code. The two
  // built-in ones are known from the start (default colours); loadProviders() adds the rest and any colour the user picked.
  const BUILTIN_PROVIDER_INFO = {
    stashdb: { key: 'stashdb', name: 'StashDB', color: '#2b6cb0', builtin: true },
    theporndb: { key: 'theporndb', name: 'ThePornDB', color: '#d69e2e', builtin: true },
  };
  let providerRegistry = { ...BUILTIN_PROVIDER_INFO };
  let providersPromise = null;
  const AUTO_PROVIDER_COLORS = ['#2f9e6e', '#a45cc4', '#cf5c78', '#3aa6b9', '#8a9a3b', '#c97b3a'];

  function providerInfo(key) {
    if (providerRegistry[key]) return providerRegistry[key];
    const sum = String(key || '').split('').reduce((n, c) => n + c.charCodeAt(0), 0);
    return { key, name: key || '?', color: AUTO_PROVIDER_COLORS[sum % AUTO_PROVIDER_COLORS.length] };
  }
  const providerLabel = (key) => providerInfo(key).name;
  // Order: StashDB, ThePornDB, then the others by name.
  const providerKeys = () => Object.keys(providerRegistry).sort((a, b) => {
    const rank = (k) => (k === 'stashdb' ? 0 : k === 'theporndb' ? 1 : 2);
    return rank(a) - rank(b) || providerLabel(a).localeCompare(providerLabel(b));
  });
  // A filter state object only lists the providers somebody switched off or on; a provider it has never heard of counts as on.
  const providerOn = (state, key) => !state || state[key] !== false;
  const providersAllOn = (state) => providerKeys().every((k) => providerOn(state, k));
  const providersSelected = (state) => (providersAllOn(state) ? null : providerKeys().filter((k) => providerOn(state, k)));

  function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || '');
    return m ? [parseInt(m[1], 16), parseInt(m[2], 16), parseInt(m[3], 16)] : [128, 128, 128];
  }
  // One <style> with a class set per provider (row tint, badge, calendar pill, legend swatch), generated from the registry colours.
  function renderProviderStyles() {
    let tag = document.getElementById('srw-provider-styles');
    if (!tag) { tag = document.createElement('style'); tag.id = 'srw-provider-styles'; document.head.appendChild(tag); }
    tag.textContent = providerKeys().map((key) => {
      const [r, g, b] = hexToRgb(providerInfo(key).color);
      const text = `rgb(${Math.round(r + (255 - r) * 0.35)}, ${Math.round(g + (255 - g) * 0.35)}, ${Math.round(b + (255 - b) * 0.35)})`;
      return `.srw-release-item-${key} { background: rgba(${r}, ${g}, ${b}, 0.14); }\n`
        + `.srw-provider-badge-${key} { background: rgba(${r}, ${g}, ${b}, 0.25); color: ${text}; }\n`
        + `.srw-cal-pill-${key} { background: rgba(${r}, ${g}, ${b}, 0.25); color: ${text}; }\n`;
    }).join('');
  }

  function loadProviders() {
    if (!providersPromise) {
      providersPromise = (async () => {
        try {
          const rows = await runOp('metadata_providers_registry');
          if (!Array.isArray(rows) || !rows.length) providersPromise = null; // sidecar not reachable yet: try again on the next call
          if (Array.isArray(rows) && rows.length) {
            providerRegistry = {};
            rows.forEach((p) => { providerRegistry[p.key] = p; });
            Object.keys(BUILTIN_PROVIDER_INFO).forEach((k) => { if (!providerRegistry[k]) providerRegistry[k] = BUILTIN_PROVIDER_INFO[k]; });
          }
        } catch (e) { /* keep the built-in two */ }
        renderProviderStyles();
        return providerRegistry;
      })();
    }
    return providersPromise;
  }
  function resetProviders() { providersPromise = null; return loadProviders(); }
  renderProviderStyles();

  let metadataProviderUrls = null;

  async function loadMetadataProviderUrls() {
    if (metadataProviderUrls) return metadataProviderUrls;
    metadataProviderUrls = {};
    await loadProviders();
    Object.values(providerRegistry).forEach((p) => { if (p.url) metadataProviderUrls[p.key] = p.url; });
    return metadataProviderUrls;
  }

  // A picture link from Stash (GraphQL image_path ...) can carry the address Stash knows itself by (an internal host); the browser has to
  // load it from the address the page was opened with, or it is blocked (Local Network Access) or unreachable. Only path and query are kept.
  function stashUrl(url) {
    if (typeof url !== 'string' || !/^https?:\/\//i.test(url)) return url;
    try {
      const u = new URL(url);
      return /^\/(performer|studio|scene|group|image|gallery|tag)\//.test(u.pathname) ? u.pathname + u.search : url;
    } catch (e) { return url; }
  }

  function sceneUrl(provider, sceneStashdbId) {
    if (provider === 'theporndb') return `https://theporndb.net/scenes/${sceneStashdbId}`;
    if (provider === 'stashdb') return `https://stashdb.org/scenes/${sceneStashdbId}`;
    const base = (metadataProviderUrls && metadataProviderUrls[provider]) || '';
    return `${base.replace(/\/graphql\/?$/, '')}/scenes/${sceneStashdbId}`;
  }

  // Shared release-row building blocks (2026-09-21) -- moved here from
  // releases.js once a THIRD file (stash-release-manager-activity.js)
  // needed the exact same thumb/entity-link/studio-context rendering;
  // releases.js/entity-panel.js now alias these instead of keeping
  // their own copies.
  function buildEntityIcons(entityTypes) {
    if (entityTypes.length === 1) {
      const icon = entityIcon(entityTypes[0]);
      if (icon) icon.setAttribute('title', `via ${entityTypes[0]}`);
      return icon;
    }
    return el('span', { className: 'srw-entity-icon-group' }, entityTypes.map((t) => {
      const icon = entityIcon(t);
      if (icon) icon.setAttribute('title', `via ${t}`);
      return icon;
    }));
  }

  // A small icon+name link to the entity's own page in *this* Stash
  // instance -- the *representative* entity's local page (same "first
  // row wins" convention groupByScene() uses), not one link per matched
  // entity type. `entity_local_id` can be null (entity since
  // unfavorited, or not yet backfilled by a sync) -- falls back to
  // plain (unlinked) text in that case rather than dropping the name
  // entirely. `entity_name` (release_repo.list_releases()'s
  // `watchlist.name` join) is the WATCHED entity's name -- for a
  // performer-matched release that's the performer, not `studio_name`
  // (always the scene's own studio regardless of which entity type
  // matched).
  // `iconOnly` (2026-09-21) -- the Calendar view's own pill already
  // shows the studio/performer name itself (`.srw-cal-pill-studio`,
  // baked into the same pill this link sits next to) -- passing this
  // through there avoids showing the name TWICE, a real regression
  // introduced when this function grew visible name text (previously
  // icon-only unconditionally, so the Calendar pill's duplicate name
  // never showed since there was nothing to duplicate).
  function buildLocalEntityLink(r, { iconOnly } = {}) {
    const name = r.entity_name || r.studio_name || '';
    if (!name) return null;
    if (!r.entity_local_id) {
      return iconOnly ? null : el('span', { className: 'srw-local-entity-name-plain', textContent: name });
    }
    const icon = entityIcon(r.entity_type);
    if (icon) icon.removeAttribute('title');
    if (iconOnly) {
      return el('a', {
        className: 'srw-local-entity-link',
        href: `/${r.entity_type}s/${r.entity_local_id}`,
        title: `Open ${name}'s page in Stash`,
      }, [icon]);
    }
    return el('a', {
      className: 'srw-local-entity-link',
      href: `/${r.entity_type}s/${r.entity_local_id}`,
      title: `Open this ${r.entity_type}'s page in Stash`,
    }, [icon, el('span', { className: 'srw-local-entity-name', textContent: name })]);
  }

  // The scene's own preview image, linking to the same scene page the
  // title does. `image_url` is only ever set for a release not yet
  // confirmed in-library (see routers/releases.py's library_status()
  // docstring on why it's cleared once satisfied), so this naturally
  // stops showing once a scene's already imported.
  function buildReleaseThumb(r) {
    if (!r.image_url) return null;
    const img = el('img', { className: 'srw-release-thumb', src: r.image_url, alt: '', loading: 'lazy' });
    const link = el('a', {
      className: 'srw-release-thumb-link',
      href: sceneUrl(r.provider, r.scene_stashdb_id),
      target: '_blank', rel: 'noopener noreferrer',
    }, [img]);
    // A hotlinked image that is blocked or gone (some source-site CDNs) leaves no broken-image icon behind.
    img.addEventListener('error', () => link.remove());
    return link;
  }

  // Scene's own studio, shown as plain (unlinked -- no local studio id
  // is available on a performer-matched row, see release_repo.py's
  // schema) context text -- only when it wouldn't just duplicate
  // buildLocalEntityLink()'s own name right next to it (i.e. when this
  // release was matched via a watched PERFORMER, not the studio
  // itself).
  function buildStudioContext(r) {
    if (!r.studio_name) return null;
    // A studio hit normally IS the scene's studio (the name is already shown). Not for a sub-studio of a watched network studio
    // (Evil Angel -> Transsexual Angel): the row names the watched parent, so the scene's own studio is shown next to it (v0.208.1).
    const compact = (t) => String(t || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
    // A row that came in through a STUDIO: the studio is the row's own entity (icon + name), so its context is the cast (v0.213.8).
    if (r.entity_type === 'studio' && (!r.entity_name || compact(r.entity_name) === compact(r.studio_name))) return buildCastContext(r);
    // Linked to the studio's Stash page when that studio exists locally (r.studio_local_id, resolved by the sidecar, v0.102.8).
    if (r.studio_local_id) {
      return el('a', {
        className: 'srw-release-studio-context srw-release-studio-link', href: `/studios/${r.studio_local_id}`,
        title: `Open ${r.studio_name} in Stash`, textContent: r.studio_name,
      });
    }
    return el('span', { className: 'srw-release-studio-context', textContent: r.studio_name });
  }

  // The performers of a scene, linked to their pages in this Stash and WITHOUT an icon (v0.213.8) -- the mirror of a performer row, which shows
  // the performer with its icon and the scene's studio beside it: a studio row shows the studio with its icon and the performers beside it.
  // r.cast = [{name, local_id}] (sidecar), else the plain '|A|B|' names; at most five, the rest as "+N" with all names in the tooltip.
  function buildCastContext(r) {
    const people = Array.isArray(r && r.cast) ? r.cast : String((r && r.cast_names) || '').split('|').filter(Boolean).map((name) => ({ name, local_id: null }));
    if (!people.length) return null;
    const wrap = el('span', { className: 'srw-release-cast', title: people.map((x) => x.name).join(', ') });
    people.slice(0, 5).forEach((x, i) => {
      if (i) wrap.appendChild(document.createTextNode(', '));
      wrap.appendChild(x.local_id
        ? el('a', { className: 'srw-release-performer-link', href: `/performers/${x.local_id}`, title: `Open ${x.name} in Stash`, textContent: x.name })
        : el('span', { textContent: x.name }));
    });
    if (people.length > 5) wrap.appendChild(document.createTextNode(` +${people.length - 5}`));
    return wrap;
  }

  // Reads the cast of the rows that have none stored yet (only wishlist lists use this; one sidecar call per 100 scenes) and fills it in.
  async function fillCast(items) {
    const todo = items.filter((r) => r && r.cast_names == null && r.provider && r.scene_stashdb_id);
    if (!todo.length) return;
    const scenes = [];
    const seen = new Set();
    todo.forEach((r) => {
      const key = `${r.provider}:${r.scene_stashdb_id}`;
      if (!seen.has(key)) { seen.add(key); scenes.push({ provider: r.provider, scene_stashdb_id: r.scene_stashdb_id }); }
    });
    try {
      for (let i = 0; i < scenes.length; i += 100) {
        const res = await runOp('releases_fill_cast', { scenes: scenes.slice(i, i + 100) });
        if (!res || !res.cast) continue;
        todo.forEach((r) => {
          const x = res.cast[`${r.provider}:${r.scene_stashdb_id}`];
          if (x) { r.cast_names = x.cast_names; r.cast_pairing = x.cast_pairing; r.cast = x.cast; }
        });
      }
    } catch (e) { /* the cast is a nicety: rows are drawn without it */ }
  }

  // A scene can be new-relative-to more than one watched entity at once
  // (e.g. a watched Studio AND a watched Performer both surface it) --
  // the backend stores one release row per (provider, scene, entity)
  // triple for that reason, but showing that as two separate list
  // entries for what's really one scene is confusing. Merges same-
  // (provider, scene_stashdb_id) rows into one display entry here, at
  // render time only -- the underlying rows are untouched, and hand-
  // off/dismiss already act on the whole scene regardless of which
  // row's ids are used. The first row encountered "wins" as the
  // representative for fields that don't vary (title/date/studio_name/
  // status/etc.) and for which entity_type/entity_stashdb_id a hand-off
  // call uses -- interchangeable for that purpose since hand-off looks
  // the scene up by scene_stashdb_id, not by which entity's row was
  // passed.
  function groupByScene(releases) {
    const map = new Map();
    releases.forEach((r) => {
      const key = `${r.provider}:${r.scene_stashdb_id}`;
      const existing = map.get(key);
      if (!existing) {
        map.set(key, { ...r, entity_types: [r.entity_type] });
      } else if (!existing.entity_types.includes(r.entity_type)) {
        existing.entity_types.push(r.entity_type);
      }
    });
    return Array.from(map.values());
  }

  // ==================== Route detection ====================

  function getRoute() {
    const path = window.location.pathname;

    const groupMatch = path.match(/\/groups\/(\d+)/);
    if (groupMatch) return { type: 'group', id: groupMatch[1] };

    const studioMatch = path.match(/\/studios\/(\d+)/);
    if (studioMatch) return { type: 'studio', id: studioMatch[1] };

    const performerMatch = path.match(/\/performers\/(\d+)/);
    if (performerMatch) return { type: 'performer', id: performerMatch[1] };

    const pluginPrefix = '/plugins/' + PLUGIN_ID;
    if (path === pluginPrefix || path.startsWith(pluginPrefix + '/')) {
      return { type: 'plugin', subpath: path.slice(pluginPrefix.length) || '/' };
    }

    return { type: 'other', path };
  }

  const navigationCallbacks = [];
  function onNavigate(callback) {
    navigationCallbacks.push(callback);
  }

  // The #fragment is ignored (v0.98.2): Settings keeps its open area there (history.replaceState), and treating that as a
  // page change rebuilt the whole plugin page on the next DOM change -- the page vanished while it reloaded.
  const pageUrl = () => window.location.href.split('#')[0];
  let lastUrl = pageUrl();

  // ---- Browser tab title (v0.231.1) ----
  // Stash titles its own pages "<Page> | <branding>" (the branding is Settings > Interface > Custom title, "Stash" when unset) but leaves the plugin
  // pages with the bare branding -- useless to tell tabs apart when several Stash tabs are open or the branding was shortened. Named the same way.
  const PAGE_TITLE = 'SRM Plugin';
  let brandingPromise = null;

  function getBranding() {
    if (!brandingPromise) {
      brandingPromise = fetch('/graphql', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: '{ configuration { ui } }' }),
      })
        .then((r) => r.json())
        .then((j) => (j.data && j.data.configuration && j.data.configuration.ui && j.data.configuration.ui.title) || 'Stash')
        .catch(() => { brandingPromise = null; return 'Stash'; });
    }
    return brandingPromise;
  }

  async function applyPageTitle() {
    if (getRoute().type !== 'plugin') return;
    const title = `${PAGE_TITLE} | ${await getBranding()}`;
    if (getRoute().type === 'plugin' && document.title !== title) document.title = title;
  }

  async function restorePageTitle() {
    // Stash normally sets the next page's own title; only undo ours if it is still showing.
    if (document.title.startsWith(`${PAGE_TITLE} | `)) document.title = await getBranding();
  }

  function checkNavigation() {
    if (pageUrl() === lastUrl) return;
    lastUrl = pageUrl();
    const route = getRoute();
    if (route.type === 'plugin') applyPageTitle(); else restorePageTitle();
    for (const cb of navigationCallbacks) {
      try { cb(route); } catch (e) { console.error(`[${PLUGIN_NAME}] navigate callback error:`, e); }
    }
  }
  // A page that changed the address itself (history.pushState to another area) and already redrew what it needs calls
  // this so the watcher does not rebuild the whole page a second time.
  function acknowledgeUrl() { lastUrl = pageUrl(); }
  function initNavigationWatcher() {
    lastUrl = pageUrl();
    // Stash can rewrite <title> after we set it (its own title handling runs again on some renders), so it is kept while on a plugin page
    applyPageTitle();
    new MutationObserver(() => { applyPageTitle(); }).observe(document.head, { childList: true, subtree: true, characterData: true });
    new MutationObserver(checkNavigation).observe(document.body, { childList: true, subtree: true });
    window.addEventListener('popstate', checkNavigation); // browser Back/Forward between areas
  }

  // ==================== DOM helpers ====================

  const BOOLEAN_ATTRS = new Set(['disabled', 'hidden', 'readonly', 'required', 'selected', 'multiple', 'open', 'autofocus']);
  function el(tag, props = {}, children = []) {
    // Secret fields (API keys, passwords) are built as plain text inputs that are only DISPLAYED masked (.srw-secret, -webkit-text-security): a
    // type=password field makes Firefox offer to save it as a login, and autocomplete="off" is ignored there (v0.230.2).
    const secret = tag === 'input' && props.type === 'password';
    if (secret) {
      props = { ...props, type: 'text', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off',
        'data-lpignore': 'true', 'data-1p-ignore': 'true', 'data-form-type': 'other' };
    }
    const node = document.createElement(tag);
    Object.entries(props).forEach(([k, v]) => {
      if (k === 'className') node.className = v;
      else if (k === 'textContent') node.textContent = v;
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2).toLowerCase(), v);
      // Boolean attributes (v0.243.20): setAttribute('disabled', false) writes the text "false", which still counts as SET -- a button built with
      // `disabled: !providers.length` was disabled although the value was false. `checked` is the property, the others are left out when falsy.
      else if (k === 'checked') node.checked = !!v;
      else if (BOOLEAN_ATTRS.has(k) && !v) { /* not set */ }
      else if (BOOLEAN_ATTRS.has(k)) node.setAttribute(k, '');
      else node.setAttribute(k, v);
    });
    if (secret) node.classList.add('srw-secret');
    (Array.isArray(children) ? children : [children]).forEach((c) => {
      if (c) node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    });
    return node;
  }

  // Generic "list of managed entries + an Add affordance" scaffold
  // (2026-09-20, Connections-tab redesign) -- extracted after Download
  // Clients and Quality Profiles (Settings page) independently
  // reimplemented near-identical refresh/list/append logic. Deliberately
  // stays this small: it only owns the container + refresh loop + empty
  // message, NOT the shape of an entry or its add control -- Download
  // Clients/Quality Profiles have "arbitrary DB rows, Add always means a
  // blank create-form", while the new Connections list has "a fixed set
  // of known types, Add means picking an unconfigured one and revealing
  // its already-existing field box" -- forcing those into one shared
  // entry/form shape would be a fake unification. `buildEntry`/
  // `buildAdder` may be async (may themselves return a Promise<Node>).
  // Callers call `await refresh()` once after setup, same as every
  // hand-rolled version of this loop already did.
  function buildManagedList(section, { fetchItems, buildEntry, buildAdder, emptyMessage }) {
    const listWrap = el('div');
    section.appendChild(listWrap);

    async function refresh() {
      listWrap.innerHTML = '';
      let items = [];
      try {
        const result = await fetchItems();
        if (Array.isArray(result)) items = result;
      } catch (e) { /* shown as an empty list below */ }
      if (items.length === 0 && emptyMessage) {
        listWrap.appendChild(el('p', { className: 'srw-hint', textContent: emptyMessage }));
      }
      for (const item of items) {
        listWrap.appendChild(await buildEntry(item, refresh));
      }
      if (buildAdder) {
        const adder = await buildAdder(refresh);
        if (adder) listWrap.appendChild(adder);
      }
    }

    return { refresh, listWrap };
  }

  // Replaces every window.confirm() in this plugin (2026-09-20, user's
  // explicit request: "ich will keine confirmation Dialoge über Chrome
  // -- das stört nur Tests und den Workflow"). Chrome's native confirm()
  // is a blocking modal that halts the whole tab until dismissed -- it
  // also breaks browser-automation tooling entirely (a stuck confirm()
  // silently no-ops every further command on that page, a real bug this
  // project hit live once, see PROGRESS.md's v0.41.3 entry). This is a
  // non-blocking inline replacement instead, generalizing the pattern
  // the review-candidate box (v0.41.8) already used ad hoc: `message` +
  // Yes/Cancel render directly next to `triggerButton` in the page, no
  // native dialog at all.
  //
  // `triggerButton` is hidden while the prompt is showing (restored on
  // Cancel; left hidden on Yes -- the caller's own `onConfirm()`
  // typically replaces the whole row/section once the action completes
  // anyway, so there's usually nothing to restore it to). `danger: true`
  // styles the Yes button red for an irreversible/destructive action
  // (deleting a file, a config row, etc.) -- the same distinction
  // `srw-btn-danger` already draws everywhere else in this plugin.
  // Names in the question ("Download \"<release name>\"?") are shortened in the middle (v0.102.4): the full name is on the
  // page right next to the box, and a long dotted release name must neither wrap nor run over the buttons.
  const CONFIRM_NAME_MAX = 44;
  function shortenQuoted(message) {
    const mid = (s) => (s.length <= CONFIRM_NAME_MAX ? s
      : `${s.slice(0, Math.ceil(CONFIRM_NAME_MAX / 2))}…${s.slice(s.length - Math.floor(CONFIRM_NAME_MAX / 2))}`);
    return String(message).replace(/"([^"]+)"|“([^”]+)”/g, (m, a, b) => (a !== undefined ? `"${mid(a)}"` : `“${mid(b)}”`));
  }

  function confirmInline(triggerButton, message, onConfirm, { danger = false } = {}) {
    // The question never takes room, so nothing around it moves (v0.238.0): a calendar pill and the buttons' column of a release row place it
    // by CSS (.srw-cal-pill / .srw-rr-actions); everywhere else it is an overlay over the trigger's own place (the trigger stays in the flow,
    // invisible, so its room is kept) -- see placeConfirm(). Without a positioned parent it replaces the trigger as it always did.
    const cssOverlay = !!triggerButton.closest('.srw-release-row .srw-rr-actions');
    const inPill = !!triggerButton.closest('.srw-cal-pill');
    const generic = !cssOverlay && !inPill && !!triggerButton.offsetParent;
    if (generic) triggerButton.style.visibility = 'hidden';
    else if (!cssOverlay) triggerButton.style.display = 'none';
    const restore = () => { triggerButton.style.display = ''; triggerButton.style.visibility = ''; };
    const prompt = el('span', { className: 'srw-inline-confirm' }, [
      // A short question stays on ONE line; only a long explanatory sentence (over ~110 characters even with the names shortened)
      // may wrap, at word boundaries (v0.103.3).
      el('span', {
        className: `srw-inline-confirm-text${shortenQuoted(message).length > 110 ? ' srw-inline-confirm-long' : ''}`,
        textContent: shortenQuoted(message), title: String(message),
      }),
      // Yes and Cancel are one unit: when the question does not fit, the TEXT wraps and the two buttons stay on one line (v0.209.2).
      el('span', { className: 'srw-inline-confirm-buttons' }, [
        el('button', {
          className: `srw-btn srw-btn-sm ${danger ? 'srw-btn-danger' : 'srw-btn-primary'}`,
          textContent: 'Yes',
          // Always restores triggerButton's visibility once onConfirm()
          // settles, success or failure -- harmless when the caller's own
          // success path rebuilds/discards the whole surrounding list
          // anyway (the usual case here), but matters when it doesn't:
          // without this, a failed action (network error, a server-side
          // `result.error`) would leave the trigger permanently hidden
          // with the prompt already gone and no way to retry.
          onClick: async () => {
            prompt.remove();
            try {
              await onConfirm();
            } finally {
              restore();
            }
          },
        }),
        el('button', {
          className: 'srw-btn srw-btn-sm',
          textContent: 'Cancel',
          onClick: () => { prompt.remove(); restore(); },
        }),
      ]),
    ]);
    triggerButton.after(prompt);
    if (generic) placeConfirm(triggerButton, prompt);
  }

  // Lays the question over the trigger's place: absolute (see .srw-confirm-overlay), at the trigger's top, anchored to the side of its containing
  // block the trigger is nearer to (so it grows into the block, not out of it), at most as wide as the block. The offsets are worked out from the
  // rectangles, so it does not matter which ancestor (a table cell, the page root, a row) is the containing block.
  function placeConfirm(trigger, prompt) {
    prompt.classList.add('srw-confirm-overlay');
    const cb = prompt.offsetParent;
    const t = trigger.getBoundingClientRect();
    let left; let top; let width;
    if (cb && cb !== document.body) {
      const r = cb.getBoundingClientRect();
      left = t.left - r.left - cb.clientLeft + cb.scrollLeft;
      top = t.top - r.top - cb.clientTop + cb.scrollTop;
      width = cb.clientWidth;
    } else { // no positioned ancestor: the page itself is the containing block
      left = t.left + window.scrollX;
      top = t.top + window.scrollY;
      width = document.documentElement.clientWidth;
    }
    prompt.style.maxWidth = `${Math.max(Math.min(width - 8, 736), 200)}px`;
    prompt.style.top = `${Math.round(top)}px`;
    if (left + t.width / 2 > width / 2) prompt.style.right = `${Math.max(Math.round(width - left - t.width), 0)}px`;
    else prompt.style.left = `${Math.round(left)}px`;
  }

  async function waitForElement(selector, { timeout = 8000, interval = 200 } = {}) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const node = document.querySelector(selector);
      if (node) return node;
      await new Promise((r) => setTimeout(r, interval));
    }
    return null;
  }

  // Same FontAwesome solid icons Stash's own top nav uses for these
  // entity types (fa-user / fa-video), copied path-for-path from the
  // live nav SVGs on 2026-09-11 rather than loading FontAwesome as a
  // dependency -- so a release's entity_type is visually distinguishable
  // (studio vs performer) the same way Stash itself distinguishes them.
  const ENTITY_ICON_PATHS = {
    performer: {
      viewBox: '0 0 448 512',
      d: 'M224 248a120 120 0 1 0 0-240 120 120 0 1 0 0 240zm-29.7 56C95.8 304 16 383.8 16 482.3 16 498.7 29.3 512 45.7 512l356.6 0c16.4 0 29.7-13.3 29.7-29.7 0-98.5-79.8-178.3-178.3-178.3l-59.4 0z',
    },
    studio: {
      viewBox: '0 0 576 512',
      d: 'M96 64c-35.3 0-64 28.7-64 64l0 256c0 35.3 28.7 64 64 64l256 0c35.3 0 64-28.7 64-64l0-256c0-35.3-28.7-64-64-64L96 64zM464 336l73.5 58.8c4.2 3.4 9.4 5.2 14.8 5.2 13.1 0 23.7-10.6 23.7-23.7l0-240.6c0-13.1-10.6-23.7-23.7-23.7-5.4 0-10.6 1.8-14.8 5.2L464 176 464 336z',
    },
  };
  const SVG_NS = 'http://www.w3.org/2000/svg';

  function entityIcon(entityType) {
    const spec = ENTITY_ICON_PATHS[entityType];
    if (!spec) return null;
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', spec.viewBox);
    svg.setAttribute('class', `srw-entity-icon srw-entity-icon-${entityType}`);
    svg.setAttribute('aria-label', entityType);
    svg.setAttribute('role', 'img');
    const path = document.createElementNS(SVG_NS, 'path');
    path.setAttribute('d', spec.d);
    path.setAttribute('fill', 'currentColor');
    svg.appendChild(path);
    return svg;
  }

  // ==================== Main Nav Link ====================

  // The top-bar entry is ONE icon among Stash's utility icons (patch point MainNavBar.UtilityItems, right of Statistics).
  // With the separate "Plugin Menu" plugin installed (github.com/northwindlab/stash-plugin-menu) this plugin only REGISTERS itself in
  // that menu (queue below, works whichever plugin loads first) and the menu draws the icon; without it a plain icon link is drawn here.
  // The check happens at render time, so installing the menu later needs only a page reload.
  const MENU_PATH = `/plugins/${PLUGIN_ID}`;
  function registerInPluginMenu() {
    window.__stashPluginMenuQueue = window.__stashPluginMenuQueue || [];
    window.__stashPluginMenuQueue.push({ id: PLUGIN_ID, label: PLUGIN_NAME, path: MENU_PATH, order: 0, badge: () => activityCount > 0 });
  }

  // Puts `item` right after the Statistics icon (or first): the utility children are ONE fragment holding the icons.
  function insertUtilityItem(React, children, item) {
    const list = React.Children.toArray(children);
    const frag = list.find((c) => c && c.type === React.Fragment);
    if (!frag) return [item, ...list];
    const inner = React.Children.toArray(frag.props.children);
    const at = inner.findIndex((c) => c && c.props && c.props.to === '/stats');
    inner.splice(at + 1, 0, item);
    return list.map((c) => (c === frag ? React.createElement(React.Fragment, { key: c.key }, ...inner) : c));
  }

  function registerNavMenuItem() {
    registerInPluginMenu();
    const PluginApi = window.PluginApi;
    if (!PluginApi || !PluginApi.patch || !PluginApi.React) return;
    // Stash re-runs plugin scripts in an open tab after reloadPlugins; every run would stack another patch. Register once per page.
    if (window.__srwNavRegistered) return;
    window.__srwNavRegistered = true;

    try {
      const React = PluginApi.React;
      const RR = PluginApi.libraries.ReactRouterDOM;
      const NavLink = RR.NavLink || RR.Link;
      const useLocation = RR.useLocation || (() => window.location);
      const { Button } = PluginApi.libraries.Bootstrap;
      const { FontAwesomeIcon } = PluginApi.libraries.ReactFontAwesome;
      const solid = PluginApi.libraries.FontAwesomeSolid;
      const icon = solid.faBoxOpen || solid.faEye;

      const renderIcon = () => {
        const location = useLocation();
        const here = location.pathname.startsWith(MENU_PATH);
        // Dot while a download/import runs (see "Activity indicator" below); the ref callback paints the current state on mount.
        const dot = React.createElement('span', { className: 'srw-nav-dot', title: 'Download or import running', hidden: true, ref: (n) => { if (n) applyActivityCount(); } });
        return React.createElement(NavLink, { className: 'nav-utility srw-nav-menu', to: MENU_PATH, isActive: () => here },
          React.createElement(Button, { className: 'minimal d-flex align-items-center h-100', title: PLUGIN_NAME }, React.createElement(FontAwesomeIcon, { icon }), dot));
      };
      const SrmIcon = () => { try { return renderIcon(); } catch (e) { console.error(`[${PLUGIN_NAME}] nav icon failed:`, e); return null; } }; // never take Stash's top bar down

      PluginApi.patch.before('MainNavBar.UtilityItems', (props) => {
        if (window.StashPluginMenu) return [props];  // the Plugin Menu plugin draws the icon (with this plugin's entry)
        const mine = (c) => c && String(c.key || '').includes('srw-nav-menu');
        const flat = React.Children.toArray(props.children);
        if (flat.some((c) => mine(c) || (c && c.props && React.Children.toArray(c.props.children).some(mine)))) return [props];
        return [{ ...props, children: insertUtilityItem(React, props.children, React.createElement(SrmIcon, { key: 'srw-nav-menu' })) }];
      });
    } catch (e) {
      console.error(`[${PLUGIN_NAME}] Failed to register main nav link:`, e);
    }
  }

  registerNavMenuItem();

  // ==================== Shared page header ====================

  // One header builder for all three of the plugin's own pages
  // (Releases / Maintenance / Settings) -- added 2026-09-16 after the
  // user noticed the cross-navigation button sat in a different spot
  // on each page (Settings had a proper title+button flex row;
  // Releases instead floated its "Settings" button inside a lower
  // toolbar alongside unrelated action buttons). Every page now calls
  // this instead of building its own header, so the nav button is
  // always in the same place and every page gets links to the other
  // two for free.
  const PAGES = [
    { key: 'releases', label: 'Releases', path: '', icon: 'faCalendarDays' },
    { key: 'activity', label: 'Activity', path: '/activity', icon: 'faDownload' },
    { key: 'history', label: 'History', path: '/history', icon: 'faClockRotateLeft' },
    { key: 'search', label: 'Search', path: '/search', icon: 'faMagnifyingGlass', kind: 'scenes' },
    { key: 'watchlist', label: 'Watchlist', path: '/watchlist', icon: 'faEye', kind: 'scenes' },
    { key: 'maintenance', label: 'Maintenance', path: '/maintenance', icon: 'faWrench' },
    { key: 'settings', label: 'Settings', path: '/settings', icon: 'faGear' },
  ];

  // Menu icon (v0.113.0): the same Font Awesome icons Stash's own menus use, taken from the library Stash hands to plugins
  // (PluginApi.libraries.FontAwesomeSolid) -- no files of our own. Returns null (no icon) when the library or the name is missing.
  function icon(name, extraClass = '') {
    try {
      const def = window.PluginApi.libraries.FontAwesomeSolid[name];
      if (!def || !def.icon) return null;
      const [width, height, , , path] = def.icon;
      const NS = 'http://www.w3.org/2000/svg';
      const svg = document.createElementNS(NS, 'svg');
      svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
      svg.setAttribute('class', `svg-inline--fa fa-icon srw-icon ${extraClass}`.trim());
      svg.setAttribute('aria-hidden', 'true');
      const p = document.createElementNS(NS, 'path');
      p.setAttribute('fill', 'currentColor');
      p.setAttribute('d', Array.isArray(path) ? path.join(' ') : path);
      svg.appendChild(p);
      return svg;
    } catch (e) { return null; }
  }

  // The age of a release in words: "34 days old", "412 days old (about 14 months)", "800 days old (about 2.2 years)" (v0.238.0).
  function ageWords(days) {
    if (days == null || !Number.isFinite(Number(days))) return 'unknown age';
    const d = Number(days);
    if (d < 90) return `${d} days old`;
    if (d < 730) return `${d} days old (about ${Math.round(d / 30.4)} months)`;
    return `${d} days old (about ${(d / 365.25).toFixed(1)} years)`;
  }

  // A release posted long before the scene's own date cannot be this scene: the warning for such a candidate (null when it fits or either date
  // is unknown). `sceneDate` = YYYY-MM-DD, `age` = days since the release was posted.
  function ageWarning(age, sceneDate) {
    if (age == null || !sceneDate) return null;
    const sceneAge = Math.floor((Date.now() - new Date(`${String(sceneDate).slice(0, 10)}T00:00:00`).getTime()) / 86400000);
    if (!Number.isFinite(sceneAge) || age <= sceneAge + 30) return null;
    return el('span', {
      className: 'srw-review-warning',
      title: `The scene is dated ${String(sceneDate).slice(0, 10)} (${Math.max(sceneAge, 0)} days ago); this release was posted ${age} days ago.`,
      textContent: 'older than the scene — probably a different release',
    });
  }

  // A button (tag 'button') or link (tag 'a') that shows only an icon; the words are the tooltip and the aria-label (v0.235.2). Saves room in
  // crowded action columns. Without the icon library the words are shown instead.
  function iconButton(tag, name, label, props = {}) {
    const { className = 'srw-btn', title, ...rest } = props;
    // `name` may be a pair [main, badge] (v0.238.5): the second icon sits small in the lower right corner of the first (e.g. fingerprint + magnifier).
    const [mainName, badgeName] = Array.isArray(name) ? name : [name, null];
    let glyph = icon(mainName);
    const badge = glyph && badgeName ? icon(badgeName, 'srw-icon-badge') : null;
    if (badge) glyph = el('span', { className: 'srw-icon-combo' }, [glyph, badge]);
    const node = el(tag, { ...(tag === 'button' ? { type: 'button' } : {}), ...rest, className: glyph ? `${className} srw-btn-icon` : className,
      title: title || label, 'aria-label': label });
    // The zero-width text gives the button the line height of a text button, so a row of them stays level.
    if (glyph) node.append(document.createTextNode('\u200b'), glyph); else node.textContent = label;
    return node;
  }

  // While an icon button's action runs: its icon becomes the spinner (the button is disabled by the caller).
  function busyIcon(btn) {
    const spin = el('span', { className: 'srw-spinner' });
    spin.setAttribute('aria-hidden', 'true');
    btn.replaceChildren(document.createTextNode('\u200b'), spin);
  }

  // One page header for every page (2026-10-04, UI review): the title and ONE tab bar in Stash's own nav-tabs look
  // (same classes as the tabs on its Performer pages, so theme/dark mode come for free). Real links -> middle click and
  // bookmarks work; this plugin's root is mounted outside Stash's React tree (see stash-release-manager.js's
  // createPluginRoot()), so a link is a normal full page load. Activity carries the live "running" count.
  // Built with the count already in it: a header is built off-page (and again whenever a page redraws), so applyActivityCount() cannot reach it
  // (the badge used to stay hidden until the next live event and then shifted the tabs). `hidden` is set as a property: el() would turn
  // hidden:false into the attribute hidden="false", which hides too.
  function activityBadge() {
    const badge = el('span', { className: 'srw-activity-badge', title: 'Downloads/imports running', textContent: String(activityCount) });
    badge.hidden = activityCount === 0;
    return badge;
  }

  // The colors of Stash's own form fields, taken from the theme (v0.240.3): a hidden `.form-control` is measured and its background / text / border
  // become --srw-field-* on <body>, with --srw-color-scheme (dark | light, from the field's brightness) for the browser's own controls (the list of an
  // open <select>, the video bar, scrollbars). The plugin's selects, text fields and the modals use them, so a theme restyles them like Stash's own.
  // Called whenever a page header or a modal is built (cheap), so a theme switch is picked up on the next page.
  function applyThemeColors() {
    try {
      const probe = document.createElement('input');
      probe.className = 'form-control';
      probe.style.cssText = 'position:absolute;visibility:hidden;left:-9999px;top:0';
      document.body.appendChild(probe);
      const cs = getComputedStyle(probe);
      let bg = cs.backgroundColor; const fg = cs.color; const border = cs.borderTopColor;
      // Stash's own secondary button (the filter bars' buttons follow it)
      const sec = document.createElement('button');
      sec.className = 'btn btn-secondary';
      sec.style.cssText = 'position:absolute;visibility:hidden;left:-9999px;top:0';
      document.body.appendChild(sec);
      const bs = getComputedStyle(sec);
      const btn = { bg: bs.backgroundColor, fg: bs.color, border: bs.borderTopColor };
      sec.remove();
      probe.remove();
      const rgb = (c) => (c.match(/[\d.]+/g) || []).map(Number);
      if (!bg || bg === 'transparent' || (rgb(bg).length === 4 && rgb(bg)[3] === 0)) bg = getComputedStyle(document.body).backgroundColor;
      const [r, g, b] = rgb(bg);
      if (r == null) return;
      const dark = (0.299 * r + 0.587 * g + 0.114 * b) < 140;
      const st = document.body.style;
      st.setProperty('--srw-field-bg', bg);
      st.setProperty('--srw-field-fg', fg);
      st.setProperty('--srw-field-border', border);
      st.setProperty('--srw-color-scheme', dark ? 'dark' : 'light');
      if (btn.bg && btn.fg) { st.setProperty('--srw-btn-bg', btn.bg); st.setProperty('--srw-btn-fg', btn.fg); st.setProperty('--srw-btn-border', btn.border); }
    } catch (e) { /* the fixed dark colors of the stylesheet stay */ }
  }

  // Copies text to the clipboard (v0.243.7). Stash is usually opened over plain http on the local network, where navigator.clipboard does not exist
  // (it needs https or localhost): then a hidden text field and the older copy command do it. Resolves to true / false.
  async function copyText(text) {
    try {
      if (window.isSecureContext && navigator.clipboard) { await navigator.clipboard.writeText(text); return true; }
    } catch (e) { /* fall through to the older way */ }
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(field);
    field.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    field.remove();
    return ok;
  }

  // Results of something the user asked for (a check, a search at a provider) are kept in the browser, so a reload or reopening a dialog does not make the
  // same request again (v0.240.12). At most 80 results; the oldest go first. Failing storage (full, blocked) just means nothing is kept.
  const RESULT_INDEX = 'srw:results';
  function keepResult(key, value) {
    try {
      const ls = window.localStorage;
      ls.setItem(`srw:result:${key}`, JSON.stringify({ at: Date.now(), value }));
      const index = (JSON.parse(ls.getItem(RESULT_INDEX) || '[]') || []).filter((k) => k !== key);
      index.push(key);
      while (index.length > 80) ls.removeItem(`srw:result:${index.shift()}`);
      ls.setItem(RESULT_INDEX, JSON.stringify(index));
    } catch (e) { /* not kept */ }
  }
  // {value, at} or null; older than maxAgeMs counts as not kept.
  function recallResult(key, maxAgeMs = 7 * 86400000) {
    try {
      const got = JSON.parse(window.localStorage.getItem(`srw:result:${key}`) || 'null');
      return got && Date.now() - got.at <= maxAgeMs ? got : null;
    } catch (e) { return null; }
  }
  function forgetResult(key) { try { window.localStorage.removeItem(`srw:result:${key}`); } catch (e) { /* ignore */ } }
  const keptNote = (at) => el('div', { className: 'srw-hint', textContent: `Kept from ${new Date(at).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' })} — press the button again to ask anew.` });

  // What the watchlist is called and drawn as, by Settings > Watchlist source (v0.240.21): only the kind the user chose is offered. The watchlist (tag mode)
  // is the eye / crossed-out eye of the browser userscript; Stash's own favorites (favorites mode) keep the heart.
  function watchKind(mode) {
    return mode === 'tag'
      ? { add: 'faEye', remove: 'faEyeSlash', addLabel: 'Add to watchlist', removeLabel: 'Remove from watchlist', on: 'on the watchlist', badge: 'faEye' }
      : { add: 'faHeart', remove: 'faHeartCrack', addLabel: 'Add to favorites', removeLabel: 'Remove from favorites', on: 'favorite', badge: 'faHeart' };
  }

  function buildPageHeader(activeKey) {
    applyThemeColors();
    const pluginPath = `/plugins/${PLUGIN_ID}`;
    const links = PAGES.map((p) => el('a', {
      className: `nav-item nav-link${p.key === activeKey ? ' active' : ''}`,
      href: pluginPath + p.path,
    }, [icon(p.icon), p.label, p.key === 'activity' ? activityBadge() : null]));
    const header = el('div', { className: 'srw-page-header' }, [
      el('div', { className: 'srw-page-title-row' }, [el('h2', { textContent: 'Release Manager' }), buildHealthPill()]),
      el('nav', { className: 'nav nav-tabs srw-main-tabs', role: 'tablist' }, links),
    ]);
    applyActivityCount();
    // Settings > Content: pages that only serve scenes (Search, Watchlist) are hidden while only movies are on (the page you are on stays).
    PAGES.forEach((p, i) => {
      if (p.kind && p.key !== activeKey) scopeAllows(p.kind).then((ok) => { if (!ok) links[i].style.display = 'none'; });
    });
    return header;
  }

  // Live health of the two things every page depends on (like stash-sense's header pill): Stash (asked through the sidecar, which
  // holds the connection) and the Release Manager sidecar itself (reachable = the plugin operation answered). The other services
  // (Prowlarr, SABnzbd, Whisparr, StashDB, ThePornDB) are checked in Settings, next to their URL/key.
  function buildHealthPill() {
    const mk = (label) => {
      const dot = el('span', { className: 'srw-status-dot' });
      return { dot, node: el('span', { className: 'srw-health-item' }, [dot, el('span', { className: 'srw-status-label', textContent: label })]) };
    };
    const stash = mk('Stash');
    const sidecar = mk('Sidecar');
    const sidecarLabel = sidecar.node.querySelector('.srw-status-label');
    const dev = mk('Development mode: no background jobs');
    dev.node.hidden = true;
    const pill = el('div', { className: 'srw-health-pill', title: 'Click to check again' }, [stash.node, sidecar.node, dev.node]);
    const set = (item, ok, tip) => {
      item.dot.className = `srw-status-dot${ok === true ? ' ok' : ok === false ? ' down' : ''}`;
      item.node.title = tip || '';
    };
    let busy = false;
    async function check() {
      if (busy) return;
      busy = true;
      try {
        const r = await runOp('connections_check', { only: 'stash' });
        if (!r || r.error) {
          set(sidecar, false, (r && r.error) || 'Sidecar not reachable');
          set(stash, null, 'Unknown (sidecar not reachable)');
        } else {
          set(sidecar, true, 'Sidecar reachable');
          runOp('sidecar_health').then((h) => {
            if (h && h.version) {
              sidecarLabel.textContent = `Sidecar v${h.version}`;
              const same = h.version === PLUGIN_VERSION;
              sidecar.node.title = same ? `Sidecar reachable, version ${h.version} (matches the plugin)`
                : `Sidecar v${h.version} but the plugin is v${PLUGIN_VERSION}: rebuild the sidecar container (docker compose up -d --build) or update the plugin so both match.`;
              if (!same) sidecar.dot.className = 'srw-status-dot warn';
            }
            dev.node.hidden = !(h && h.dev_mode);
            dev.dot.className = 'srw-status-dot warn';
            dev.node.title = 'This sidecar was started with SRM_DEV_MODE: it runs no scan, search, import or sync job (manual actions still work).';
          }).catch(() => {});
          const s = r.stash || {};
          set(stash, !!s.ok, s.ok ? 'Stash reachable' : (s.error || 'Stash not reachable from the sidecar'));
        }
      } catch (e) {
        set(sidecar, false, e.message);
        set(stash, null, 'Unknown');
      } finally { busy = false; }
    }
    pill.addEventListener('click', check);
    check();
    // Re-check every 60 s while this header is on the page.
    const timer = setInterval(() => { if (pill.isConnected) check(); else clearInterval(timer); }, 60000);
    return pill;
  }

  // Second level for pages with many areas (Settings, later Maintenance): Stash's own Settings look (vertical pills,
  // always visible, one area open at a time). `items`: [{key, label, badge?}]; `onSelect(key)` is called on a click.
  // Returns {nav, layout(contentNode), setActive(key)}.
  // Settings (and tools) that do nothing under the chosen Whisparr scope are greyed out (the `inert` attribute: no clicks, no focus) with a
  // line saying why (v0.149.0, shared since v0.156.0) -- never hidden, so the setting is still found and its value kept. Only what the code
  // really ignores is registered: the movies always use this sidecar's own search, download and import, whatever the preset says.
  // registry.add(element, [presets that make it inert], reason); registry.apply(preset).
  function createScopeStateRegistry() {
    const entries = [];
    return {
      add(element, inertIn, reason) {
        if (!element) return;
        const hint = el('p', { className: 'srw-hint srw-inert-hint', textContent: reason, hidden: true });
        const first = element.firstElementChild;
        // after the heading (h4 of a section, or the sub-heading of a block), before the controls
        if (first && (first.tagName === 'H4' || first.classList.contains('srw-subsection-heading'))) first.after(hint); else element.prepend(hint);
        entries.push({ element, inertIn, hint });
      },
      apply(preset) {
        entries.forEach(({ element, inertIn, hint }) => {
          const inert = inertIn.includes(preset);
          element.inert = inert;
          element.classList.toggle('srw-inert', inert);
          hint.hidden = !inert;
        });
      },
    };
  }

  // `items`: links ({key, label, icon?, href?, badge?}) and, optionally, group headings ({heading: 'Text'}) -- a muted line over the links
  // that follow (v0.153.0, Maintenance). setBadge(key, n) updates the number behind a link (0/empty removes it).
  function buildSideNav(items, activeKey, onSelect) {
    const links = new Map();
    const badgeOf = (n) => (n ? el('span', { className: 'srw-sidenav-badge', textContent: String(n) }) : null);
    const nav = el('nav', { className: 'nav nav-pills flex-column srw-sidenav' }, items.map((it) => {
      if (it.heading) return el('div', { className: 'srw-sidenav-heading', textContent: it.heading });
      const a = el('a', { className: 'nav-link', href: it.href || `${location.pathname}${location.search}#${it.key}` }, [it.icon ? icon(it.icon) : null, it.label, badgeOf(it.badge)]);
      a.addEventListener('click', (e) => { e.preventDefault(); onSelect(it.key); });
      links.set(it.key, a);
      return a;
    }));
    const setActive = (key) => links.forEach((a, k) => a.classList.toggle('active', k === key));
    const setBadge = (key, n) => {
      const a = links.get(key);
      if (!a) return;
      const old = a.querySelector('.srw-sidenav-badge');
      if (old) old.remove();
      const fresh = badgeOf(n);
      if (fresh) a.appendChild(fresh);
    };
    setActive(activeKey);
    return { nav, setActive, setBadge, layout: (content) => el('div', { className: 'srw-side-layout' }, [nav, el('div', { className: 'srw-side-content' }, [content])]) };
  }

  // Tabs over a set of panes (v0.102.2) -- ONE component for every page whose sections used to be fold-away panels
  // (Activity, Releases, the Releases tab on Performer/Studio pages). `items`: [{key, label (string | () => string),
  // pane (element), watch? (element whose text changes when the label's numbers do)}]. Panes not selected get `hidden`.
  // The choice is remembered in the browser under `storeKey`. Returns the tab row.
  function buildPaneTabs(items, storeKey, preferredKey = null) {
    const btns = items.map((it) => {
      const b = el('button', { type: 'button', className: 'srw-btn' });
      const set = () => { b.textContent = typeof it.label === 'function' ? it.label() : it.label; };
      set();
      if (it.watch) new MutationObserver(set).observe(it.watch, { childList: true, characterData: true, subtree: true });
      b.addEventListener('click', () => select(it.key));
      return b;
    });
    let store = null;
    try { store = window.localStorage; } catch (e) { /* ignore */ }
    function select(key) {
      items.forEach((it, i) => { it.pane.hidden = it.key !== key; btns[i].classList.toggle('srw-btn-primary', it.key === key); });
      try { store && store.setItem(storeKey, key); } catch (e) { /* ignore */ }
      const shown = items.find((it) => it.key === key);
      if (shown && shown.onShow) shown.onShow(); // (v0.169.0) e.g. a list that loads itself when its tab is opened
    }
    let remembered = null;
    try { remembered = store && store.getItem(storeKey); } catch (e) { /* ignore */ }
    select([remembered, preferredKey, items[0].key].find((k) => k && items.some((it) => it.key === k)));
    return el('div', { className: 'srw-subtabs srw-section-tabs' }, btns);
  }

  // Label for a fold-away section turned tab: `short` name plus the number of its live heading ("Stuck (searched…) — 30" -> "Stuck (30)").
  function sectionTabLabel(details, short) {
    const m = (((details.querySelector('summary') || {}).textContent) || '').match(/—\s*(.*)$/);
    return `${short}${m ? ` (${m[1].trim()})` : ''}`;
  }

  // ==================== Activity indicator ====================
  //
  // A number on the Activity tab and a dot on this plugin's entry in Stash's top bar while downloads/imports run.
  // Fed by the sidecar's live 'progress' events (map {scene: progress|null}); a non-null entry is a running download
  // or import. No polling here: without the live stream (browser can't reach the sidecar) the indicator stays hidden.
  // Every page is a full page load, so the number is kept between them (localStorage) and shown at once; the first live event or the one
  // request below then corrects it. Without that the badge appeared half a second after the menu and pushed the tabs aside.
  const ACTIVITY_COUNT_KEY = 'srw:activity-count';
  let activityCount = 0;
  try { activityCount = Math.max(0, parseInt(window.localStorage.getItem(ACTIVITY_COUNT_KEY), 10) || 0); } catch (e) { /* no storage */ }
  function setActivityCount(n) {
    activityCount = n;
    try { window.localStorage.setItem(ACTIVITY_COUNT_KEY, String(n)); } catch (e) { /* no storage */ }
    applyActivityCount();
  }
  function applyActivityCount() {
    document.querySelectorAll('.srw-activity-badge').forEach((n) => {
      n.textContent = String(activityCount);
      n.hidden = activityCount === 0;
    });
    document.querySelectorAll('.srw-nav-dot').forEach((n) => { n.hidden = activityCount === 0; });
    window.dispatchEvent(new CustomEvent('spm-change'));  // the Plugin Menu plugin (if installed) re-reads this plugin's badge
  }
  function startActivityWatch() {
    let pushed = false; // a live event is newer than the request below
    subscribeLive((msg) => {
      if (msg.type === 'progress') {
        pushed = true;
        setActivityCount(Object.values(msg.data || {}).filter(Boolean).length);
      }
    });
    // The live stream needs a moment (and may not exist); ask once so the number is right on every page.
    runOp('releases_download_progress').then((data) => {
      if (!pushed && data && !data.error) setActivityCount(Object.values(data).filter(Boolean).length);
    }).catch(() => {});
  }

  // "Search visibility" (Milestone 3, 2026-09-19) -- mirrors
  // indexer_search.py's RESULT_* constants. Shared here (not defined
  // per-page) since both the Releases page's "Sent to Whisparr" list
  // and the entity panel's "Missing from Catalog" list show it for the
  // same underlying release rows.
  const SEARCH_RESULT_LABEL = {
    no_quality_profile: 'no quality profile configured',
    vr_disabled: 'VR search not enabled',
    search_failed: 'search failed',
    provider_lookup_failed: 'could not check the scene at the provider -- tried again at the next run',
    search_backend_unavailable: 'search backend not available (Settings > Indexers)',
    no_candidates: 'no indexer results',
    title_mismatch_only: 'results found, none matched the title',
    no_quality_match: 'results found, none matched quality/VR',
    // 2026-09-28: one download per scene; same scene imported via another provider.
    already_downloading: 'a download for this scene is already running',
    duplicate_download: 'the same release was already sent to SABnzbd (queue or history) -- not fetched again',
    daily_grab_cap: 'the daily grab limit is reached (setting max_grabs_per_day)',
    already_in_library: 'already in the library (via another provider)',
    needs_review: 'possible match — needs review',
    no_download_client: 'no download client configured',
    client_not_implemented: 'download client type not supported yet',
    grab_failed: 'grab failed',
    grabbed: 'grabbed',
  };

  // `downloadJobId` set takes priority over `lastSearchResult` -- a
  // later re-check might have found something after an earlier miss
  // was recorded, and the job id is the more current/authoritative
  // signal of the two.
  function searchStatusLabel(downloadJobId, lastSearchResult) {
    if (downloadJobId) return 'grabbed';
    if (!lastSearchResult) return 'not searched yet';
    return SEARCH_RESULT_LABEL[lastSearchResult] || lastSearchResult;
  }

  // The `sent_to_whisparr` release status's own user-facing wording
  // (2026-09-20, user request) -- this used to read "Sent to Whisparr"
  // everywhere, which overstates Whisparr's actual role: Milestone 3
  // means THIS SIDECAR runs the real search (indexer_search.py),
  // Whisparr is only ever registered with for bookkeeping (see
  // handoff.py's send_to_whisparr() docstring) -- "Sent to Whisparr"
  // implied Whisparr itself was doing something active, when it isn't.
  // The underlying DB `status` column value stays `sent_to_whisparr`
  // (an internal enum, not user-facing, not worth the much larger risk
  // of renaming a CHECK-constrained column + every query against it
  // for a wording fix) -- only the two DISPLAY strings change, and only
  // ONE place needs to (not the two separate hardcoded literals this
  // replaces), so a future Settings toggle for how much of the
  // pipeline stays with Whisparr (queued, not built yet) has a single
  // spot to plug into instead of a hunt-and-replace across the plugin.
  const SENT_STATUS_LABEL = 'On Wishlist'; // compact form -- legend swatches, title tooltips
  const SENT_STATUS_LABEL_LONG = 'On Wishlist (awaiting download)'; // section headers

  // ==================== Notifications ====================

  // Floating, non-blocking notification cards (2026-09-26, user request:
  // like Stasharr's / Portainer's -- not a browser alert(), and it stays
  // until the user closes it). Stacked top-right below Stash's navbar;
  // the oldest is dropped beyond MAX_NOTIFICATIONS. type: 'success' |
  // 'info' | 'error'.
  const MAX_NOTIFICATIONS = 6;
  // Auto-close (2026-09-28, user request): info/success cards close after
  // NOTIFY_AUTO_CLOSE_MS, paused while the pointer is over them; errors
  // (and warnings) stay until closed -- and are also written to the
  // History/Events log (history_add; everything else a pop-up confirms is
  // logged by the sidecar itself, so it isn't duplicated here).
  const NOTIFY_AUTO_CLOSE_MS = 8000;

  function notify(title, message = '', type = 'info') {
    if (type === 'error' || type === 'warning') {
      runOp('history_add', { level: type, title, message: message || title }).catch(() => {});
    }
    let host = document.getElementById('srw-notifications');
    if (!host) {
      host = el('div', { id: 'srw-notifications', className: 'srw-notifications' });
      document.body.appendChild(host);
    }
    const card = el('div', { className: `srw-notification srw-notification-${type}`, role: 'status' }, [
      el('button', {
        className: 'srw-notification-close', title: 'Close', textContent: '×',
        onClick: () => card.remove(),
      }),
      el('div', { className: 'srw-notification-title', textContent: title }),
      message ? el('div', { className: 'srw-notification-message', textContent: message }) : null,
    ]);
    host.appendChild(card);
    while (host.children.length > MAX_NOTIFICATIONS) host.firstChild.remove();
    if (type !== 'error' && type !== 'warning') {
      let timer = null;
      const arm = () => { timer = setTimeout(() => card.remove(), NOTIFY_AUTO_CLOSE_MS); };
      card.addEventListener('mouseenter', () => clearTimeout(timer));
      card.addEventListener('mouseleave', arm);
      arm();
    }
    return card;
  }

  // Dismiss with a way back (v0.104.0): after any Dismiss, a card with "Undo" stays for UNDO_MS. `undo` is an async function that
  // reverses the dismiss and returns the usual runOp result ({error} on failure); `onChange` re-renders the page after the undo.
  const UNDO_MS = 12000;

  function dismissedNotice(title, message, undo, onChange) {
    let host = document.getElementById('srw-notifications');
    if (!host) {
      host = el('div', { id: 'srw-notifications', className: 'srw-notifications' });
      document.body.appendChild(host);
    }
    let timer = null;
    const undoBtn = el('button', { className: 'srw-btn', textContent: 'Undo' });
    const card = el('div', { className: 'srw-notification srw-notification-info srw-notification-undo', role: 'status' }, [
      el('button', { className: 'srw-notification-close', title: 'Close', textContent: '×', onClick: () => card.remove() }),
      el('div', { className: 'srw-notification-title', textContent: title }),
      message ? el('div', { className: 'srw-notification-message', textContent: message }) : null,
      el('div', { className: 'srw-notification-actions' }, [undoBtn]),
    ]);
    undoBtn.addEventListener('click', async () => {
      clearTimeout(timer);
      undoBtn.disabled = true;
      let result = null;
      try { result = await undo(); } catch (e) { result = { error: e.message }; }
      card.remove();
      if (result && result.error) { notify('Undo', `Undo failed: ${result.error}`, 'error'); return; }
      notify('Undo', `Restored: ${title}`, 'success');
      if (onChange) await onChange();
    });
    const arm = () => { timer = setTimeout(() => card.remove(), UNDO_MS); };
    card.addEventListener('mouseenter', () => clearTimeout(timer));
    card.addEventListener('mouseleave', arm);
    host.appendChild(card);
    while (host.children.length > MAX_NOTIFICATIONS) host.firstChild.remove();
    arm();
    return card;
  }

  // runOp('releases_dismiss') that never throws: a failed request (HTTP/GraphQL error, timeout) comes back as {error}, so the caller
  // shows "Dismiss failed" instead of an unhandled rejection that leaves the user guessing whether anything was saved.
  async function dismissRelease(release) {
    try { return await runOp('releases_dismiss', { provider: release.provider, scene_stashdb_id: release.scene_stashdb_id }); }
    catch (e) { return { error: e.message || String(e) }; }
  }

  // "Dismiss" for a release on the wishlist that nothing was grabbed for (Activity > Stuck, Releases > On Wishlist; v0.213.5): the scene is not
  // wanted after all. Same Undo card as everywhere; a Whisparr entry that is monitored is unmonitored by the sidecar.
  // Dismiss (and, for a movie that came from an empty Stash group, "Dismiss & delete group") for a wanted/suggested movie -- one function for the
  // Movies page and Activity > Stuck (v0.214.1). The group delete asks first (a look into the group now); a group that holds files after all gets a
  // stronger question. Only the group goes, never scenes or files; Undo brings the movie back, not the group. `onChanged()` redraws the list.
  function buildFilmDismissButtons(f, onChanged) {
    const out = [];
    if (!['suggested', 'wanted'].includes(f.status)) return out;
    const done = async () => { if (onChanged) await onChanged(); };
    out.push(el('button', { className: 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss', textContent: 'Dismiss',
      title: 'Not wanted after all: off the wishlist (with Undo)',
      onClick: async (e) => {
        e.currentTarget.disabled = true;
        const res = await runOp('films_set_status', { id: f.id, status: 'dismissed' });
        if (res.error) { notify('Movie', res.error, 'error'); e.currentTarget.disabled = false; return; }
        dismissedNotice(`Dismissed: ${f.title}`, '', () => runOp('films_set_status', { id: f.id, status: res.previous }), () => { if (onChanged) onChanged(); });
        await done();
      } }));
    if (f.source === 'empty-group' && f.stash_group_id) {
      const delBtn = el('button', { className: 'btn btn-danger srw-btn-fit srw-btn-dismiss', textContent: 'Dismiss & Delete',
        title: 'Dismiss this movie and delete its empty group in Stash (scenes and files are never deleted)' });
      delBtn.addEventListener('click', async () => {
        delBtn.disabled = true;
        const probe = await runOp('films_dismiss_delete_group', { id: f.id, dry_run: true });
        delBtn.disabled = false;
        if (probe.error) { notify('Delete group', probe.error, 'error'); return; }
        if (!probe.exists) { notify('Delete group', 'The group does not exist in Stash any more -- use Dismiss.', 'info'); return; }
        const hasFiles = probe.files > 0;
        const names = (probe.file_list || []).map((x) => x.split('/').pop()).join(', ');
        const msg = hasFiles
          ? `The group "${probe.name}" is NOT empty: ${probe.files} file(s) in ${probe.scenes} scene(s) (${names}${probe.files > probe.file_list.length ? ', …' : ''}). Delete only the group (scenes and files stay in Stash) and dismiss "${f.title}"?`
          : `Dismiss "${f.title}" and delete the empty group "${probe.name}" in Stash?`;
        confirmInline(delBtn, msg, async () => {
          const res = await runOp('films_dismiss_delete_group', { id: f.id, dry_run: false, force: hasFiles });
          if (res.error) { notify('Delete group', res.error, 'error'); return; }
          dismissedNotice(`Dismissed: ${f.title} -- group ${f.stash_group_id} deleted`, `Backup of the group: data/${res.backup}. Undo brings the movie back, not the group.`,
            () => runOp('films_set_status', { id: f.id, status: res.previous }), () => { if (onChanged) onChanged(); });
          await done();
        }, { danger: true });
      });
      out.push(delBtn);
    }
    return out;
  }

  function buildWishlistDismiss(release, onChanged) {
    return el('button', {
      className: 'btn btn-outline-danger srw-btn-fit srw-btn-dismiss', textContent: 'Dismiss',
      title: 'Not wanted after all: off the wishlist, no more searching (with Undo)',
      onClick: async (e) => {
        e.currentTarget.disabled = true;
        const res = await dismissRelease(release);
        if (res && res.error) { notify('Dismiss', `Dismiss failed: ${res.error}`, 'error'); e.currentTarget.disabled = false; return; }
        releaseDismissedNotice(release, res, () => { if (onChanged) onChanged(); });
        if (onChanged) await onChanged();
      },
    });
  }

  // The Undo card for a release dismiss. `result` is the dismiss op's answer ({previous: rows as they were, or {error}}).
  function releaseDismissedNotice(release, result, onChange) {
    if (result && result.error) { notify('Dismiss', `Dismiss failed: ${result.error}`, 'error'); return false; }
    const rows = (result && result.previous) || [];
    const name = String(release.title || release.scene_stashdb_id || '');
    dismissedNotice(`Dismissed: ${name.length > 70 ? name.slice(0, 69) + '…' : name}`, '',
      () => (rows.length
        ? runOp('releases_restore', { rows })
        : runOp('releases_undismiss', { provider: release.provider, scene_stashdb_id: release.scene_stashdb_id })),
      onChange);
    if (result && result.suggest_hide) suggestHideNotice(result.suggest_hide, onChange);
    return true;
  }

  // After 10 dismisses of one studio in a row without a release of it on the wishlist (v0.216.0, dismiss_streak.py): offer to hide the studio instead.
  // A "no" (Not now, the cross, or just ignoring the card) means the next offer comes after 10 more dismisses.
  function suggestHideNotice(s, onChange) {
    let host = document.getElementById('srw-notifications');
    if (!host) { host = el('div', { id: 'srw-notifications', className: 'srw-notifications' }); document.body.appendChild(host); }
    const hideBtn = el('button', { className: 'srw-btn srw-btn-primary', textContent: 'Hide studio' });
    const card = el('div', { className: 'srw-notification srw-notification-info', role: 'status' }, [
      el('button', { className: 'srw-notification-close', title: 'Close', textContent: '×', onClick: () => card.remove() }),
      el('div', { className: 'srw-notification-title', textContent: `Hide “${s.name || 'this studio'}”?` }),
      el('div', { className: 'srw-notification-message',
        textContent: `You dismissed ${s.count} releases of this studio in a row without adding one to the wishlist. Hide the studio instead of dismissing the next ones? (Settings > Hidden studios brings it back.)` }),
      el('div', { className: 'srw-notification-actions' }, [hideBtn, el('button', { className: 'srw-btn', textContent: 'Not now', onClick: () => card.remove() })]),
    ]);
    hideBtn.addEventListener('click', async () => {
      hideBtn.disabled = true;
      const result = await runOp('muted_studios_mute', { studio_id: s.studio_id, name: s.name, provider: 'stashdb' });
      card.remove();
      if (result.error) { notify('Hide studio', result.error, 'error'); return; }
      dismissedNotice('Studio hidden', `“${result.name || s.name}” hidden (${result.hidden} release(s)).`,
        () => runOp('muted_studios_unmute', { studio_id: s.studio_id }), () => { if (onChange) onChange(); });
      if (onChange) await onChange();
    });
    host.appendChild(card);
    while (host.children.length > MAX_NOTIFICATIONS) host.firstChild.remove();
    return card;
  }

  // Standard notification for an "Add to Wishlist" result (the handoff
  // endpoints' {already_sent, grabbed, search_result} shape).
  // "Re-add" of a scene Stash already has (Search page, Maintenance > Duration mismatch): the answer of readd_library_scene().
  function notifyReadd(sceneTitle, result) {
    const name = sceneTitle || 'Scene';
    if (result.search_error) {
      notify(`Re-added: ${name}`, `Queued for the scheduled upgrade search. The search just now failed: ${result.search_error}`, 'warning');
    } else if (result.grabbed) {
      notify(`Grabbed: ${name}`, `${result.grabbed.title} (${result.grabbed.indexer || 'indexer'}) -- a verified download replaces the old file.`, 'success');
    } else if (result.search_running) {
      notify(`Re-added: ${name}`, 'The search takes a while (slow indexer) and goes on in the background -- see Activity and History.', 'success');
    } else if (result.search_result) {
      notify(`Re-added: ${name}`, `Nothing taken yet (${searchStatusLabel(null, result.search_result)}). It stays on the upgrade list and is searched again by the schedule.`, 'info');
    } else {
      notify(`Re-added: ${name}`, 'On the upgrade list; the scheduled search looks for it (Maintenance > Upgrade wanted).', 'success');
    }
  }

  function notifyHandoff(sceneTitle, result) {
    const name = sceneTitle || 'Scene';
    if (result.already_in_library) {
      notify(`Already in the library: ${name}`, 'Stash already has it (or it was imported earlier via another provider) -- not searched.', 'info');
    } else if (result.grabbed) {
      notify(`Grabbed: ${name}`, `${result.grabbed.title} (${result.grabbed.indexer || 'indexer'}) -- sent to the download client.`, 'success');
    } else if (result.already_sent) {
      notify(`Already on the wishlist: ${name}`, result.search_result
        ? `Searched again: ${searchStatusLabel(null, result.search_result)}.`
        : (result.search_running ? 'The search takes a while (slow indexer) and goes on in the background -- the result shows under Activity and History.' : ''), 'info');
    } else if (result.search_running) {
      notify(`Added to wishlist: ${name}`, 'The search takes a while (slow indexer) and goes on in the background -- the result shows under Activity and History.', 'success');
    } else if (result.search_skipped) {
      notify(`Added to wishlist: ${name}`, 'Not searched now -- the scheduled search will find it. "Search now" looks right away.', 'success');
    } else if (!result.search_result) {
      notify(`Added to wishlist: ${name}`, 'Whisparr is searching for it.', 'success');
    } else {
      notify(`Added to wishlist: ${name}`,
        `${searchStatusLabel(null, result.search_result)} -- it keeps being searched automatically.`, 'success');
    }
  }

  // "Add" next to "Add + search" (v0.205.0): puts the release on the wishlist WITHOUT asking the indexers now -- the scheduled search finds it
  // later. One shared button for the list views; `onDone()` redraws the list. (The status annotation removes it again for releases that are
  // already tracked, together with the "Add + search" button's change to "Search now".)
  function buildAddOnlyButton(release, onDone) {
    const btn = el('button', {
      className: 'srw-btn srw-whisparr-add-only', textContent: 'Add',
      title: 'Put it on the wishlist without searching now -- the scheduled search finds it later',
    });
    btn.addEventListener('click', () => confirmInline(btn,
      `Add "${release.title || release.scene_stashdb_id}" to your wishlist? It is searched by the schedule, not right now.`, async () => {
        const result = await runOp('releases_handoff', {
          provider: release.provider, entity_type: release.entity_type,
          entity_stashdb_id: release.entity_stashdb_id, scene_stashdb_id: release.scene_stashdb_id, search: false,
        });
        if (result.error) { notify(`Adding failed: ${release.title || ''}`, result.error, 'error'); return; }
        notifyHandoff(release.title, result);
        if (onDone) await onDone();
      }));
    return btn;
  }

  // ==================== Remembered open/closed state ====================

  // Remembers whether a <details> section was open, per browser (2026-09-25,
  // user request: "die Ansichten sollen sich merken, was aufgeklappt war").
  // localStorage, not the sidecar's DB: purely a per-viewer display
  // convenience, and it must never break a page -- every access is
  // wrapped, a blocked/cleared storage just falls back to `defaultOpen`.
  // Only the user's OWN toggles are stored; applying the restored/default
  // state here fires a `toggle` event too, which is skipped on purpose so
  // an untouched section keeps following its caller's default (e.g.
  // "newest year open") instead of freezing whatever it was first shown as.
  // Other `toggle` listeners (lazy loaders) still fire normally.
  function rememberOpen(details, key, defaultOpen = details.open) {
    const storageKey = `srw-open:${key}`;
    let stored = null;
    try { stored = localStorage.getItem(storageKey); } catch (e) { /* storage unavailable */ }
    const wanted = stored === null ? defaultOpen : stored === '1';
    let skipEventFor = wanted !== details.open ? wanted : null;
    details.open = wanted;
    details.addEventListener('toggle', () => {
      if (skipEventFor !== null && details.open === skipEventFor) {
        skipEventFor = null;
        return;
      }
      skipEventFor = null;
      try { localStorage.setItem(storageKey, details.open ? '1' : '0'); } catch (e) { /* ignore */ }
    });
    return details;
  }

  // ==================== Paging ====================

  // The ONE pager every paged list uses (2026-10-04): Prev / "Page [n v] of Y (total)" / Next. The select jumps
  // straight to a page, limited to 15 pages before and 15 after the current one (like Stash's own jump list, but
  // without 300 entries on a long list). `onPage(n)` gets the new page number; `total` (item count) is optional.
  const PAGER_JUMP_SPAN = 15;
  function buildPager({ page, totalPages, total = null, perPage = null, onPage }) {
    // Stash's own look (v0.151.0): joined buttons « ‹ [1 of 23 ▾] › » and the range "1-40 of 893" underneath.
    const step = (label, target, title) => {
      const btn = el('button', { type: 'button', className: 'srw-pager-btn', textContent: label, title });
      // Property assignment, not an el() prop -- setAttribute('disabled', false) would still leave the attribute present.
      btn.disabled = target < 1 || target > totalPages || target === page;
      btn.addEventListener('click', () => onPage(target));
      return btn;
    };
    const lo = Math.max(1, page - PAGER_JUMP_SPAN);
    const hi = Math.min(totalPages, page + PAGER_JUMP_SPAN);
    let current;
    if (hi > lo) {
      // Own dropdown instead of a native <select> (2026-10-04): the native popup could hang the page when the pager
      // was rebuilt while it was still open. The list is a plain element, closed BEFORE the page changes, and the
      // page change itself runs after the click handler returned.
      const toggle = el('button', { type: 'button', className: 'srw-pager-jump', title: `Jump to a page (${PAGER_JUMP_SPAN} before / ${PAGER_JUMP_SPAN} after this one)`, textContent: `${page} of ${totalPages} ▾` });
      const menu = el('div', { className: 'srw-pager-menu', hidden: true });
      const close = () => { menu.hidden = true; document.removeEventListener('mousedown', onOutside, true); document.removeEventListener('keydown', onKey, true); };
      const onOutside = (e) => { if (!current.contains(e.target)) close(); };
      const onKey = (e) => { if (e.key === 'Escape') close(); };
      for (let n = lo; n <= hi; n += 1) {
        const item = el('button', { type: 'button', className: `srw-pager-menu-item${n === page ? ' active' : ''}`, textContent: String(n) });
        item.addEventListener('click', () => {
          close();
          if (n !== page) setTimeout(() => onPage(n), 0);
        });
        menu.appendChild(item);
      }
      toggle.addEventListener('click', () => {
        if (!menu.hidden) { close(); return; }
        menu.hidden = false;
        document.addEventListener('mousedown', onOutside, true);
        document.addEventListener('keydown', onKey, true);
        const active = menu.querySelector('.active');
        if (active) menu.scrollTop = active.offsetTop - menu.clientHeight / 2;
      });
      current = el('span', { className: 'srw-pager-jump-wrap' }, [toggle, menu]);
    } else {
      current = el('span', { className: 'srw-pager-jump-wrap srw-pager-static', textContent: `${page} of ${totalPages}` });
    }
    const group = el('div', { className: 'srw-pager-group' }, [
      step('«', 1, 'First page'), step('‹', page - 1, 'Previous page'), current, step('›', page + 1, 'Next page'), step('»', totalPages, 'Last page'),
    ]);
    const parts = [group];
    if (total !== null) {
      const range = perPage ? `${Math.min((page - 1) * perPage + 1, total)}-${Math.min(page * perPage, total)} of ${total}` : `${total} in total`;
      parts.push(el('div', { className: 'srw-pager-range', textContent: range }));
    }
    return el('div', { className: 'srw-pager' }, parts);
  }

  // Client-side paging for a long flat list (2026-09-25, Activity page) --
  // same Prev/Next/"Page X of Y" look and 40-per-page size as the
  // Watchlist/Search pages. `renderItem(item)` returns one <li>; the list
  // re-renders in place on page change. Below `pageSize` items no pager
  // is shown at all.
  function buildPagedList(items, renderItem, { pageSize = 40, listClass = 'srw-release-list' } = {}) {
    const wrap = el('div');
    const totalPages = Math.max(1, Math.ceil(items.length / pageSize));
    let page = 1;
    function pager() {
      return buildPager({ page, totalPages, total: items.length, perPage: pageSize, onPage: (n) => { page = Math.min(Math.max(n, 1), totalPages); draw(); } });
    }
    function draw() {
      wrap.innerHTML = '';
      const ul = el('ul', { className: listClass });
      items.slice((page - 1) * pageSize, page * pageSize).forEach((item) => ul.appendChild(renderItem(item)));
      if (totalPages > 1) wrap.appendChild(pager());
      wrap.appendChild(ul);
      if (totalPages > 1) wrap.appendChild(pager());
    }
    draw();
    return wrap;
  }

  // ==================== Search actions ====================

  // Which search backends are usable right now (2026-09-25, Indexers-tab
  // stage 3) -- GET /settings/search-backend, fetched once per page load
  // and shared by every release row (dozens per page) instead of one
  // request each. `force` re-fetches, e.g. after a Settings change.
  let searchCapsPromise = null;
  function getSearchCapabilities(force = false) {
    if (!searchCapsPromise || force) {
      searchCapsPromise = runOp('settings_search_backend').catch((e) => ({ error: e.message }));
    }
    return searchCapsPromise;
  }

  // Manual search actions for a release the automatic search didn't
  // grab anything for (2026-09-25, replaces releases.js's old
  // buildWhisparrFallbackControls()/buildProwlarrSearchControls(), which
  // showed both buttons regardless of configuration and only existed on
  // the Releases page). Shared by the Releases page and the Studio/
  // Performer panel. Depends on what's configured:
  // - Whisparr configured -> "Search via Whisparr" (its own automatic
  //   search+grab for this movie, see handoff.py's fallback_to_whisparr()).
  // - Prowlarr and/or direct indexers configured -> "Search…" panel with
  //   an own query; a source picker appears when both exist (user
  //   decision: manual search offers every sidecar source, independent
  //   of the automatic search backend).
  // Returns one span right away and fills it once the capabilities
  // arrive, so callers can stay synchronous. `onGrabbed` runs after a
  // successful manual grab (caller re-renders).
  // The safety check of the moving/renaming functions (api/renamer_check.py): other renaming plugins in Stash, library folders, formats, ...
  // `compact`: only warnings/errors (for Import folder and Organize library); the full list is Settings > Import & Naming > Safety check.
  function buildSafetyPanel({ compact = false } = {}) {
    const box = el('div', { className: 'srw-safety' });
    const status = el('span', { className: 'srw-key-status' });
    const MARK = { error: '✖', warning: '⚠', info: 'ℹ', ok: '✔' };
    async function load() {
      status.textContent = ' checking…';
      const r = await runOp('sanity_check', renamerBlocked() ? { renamers: true } : {});
      status.textContent = '';
      if (r.error) { box.replaceChildren(el('p', { className: 'srw-hint', textContent: `The safety check could not run: ${r.error}` })); return; }
      const shown = (r.issues || []).filter((i) => !compact || i.level === 'error' || i.level === 'warning');
      if (compact && !shown.length) { box.replaceChildren(); return; }
      const parts = [el('ul', { className: 'srw-release-list' }, shown.map((i) => el('li', { className: `srw-safety-${i.level}` }, `${MARK[i.level] || ''} ${i.text}`)))];
      if ((r.renamers || []).some((x) => x.enabled)) {
        const cb = el('input', { type: 'checkbox' });
        cb.checked = ignoreRenamers();
        cb.addEventListener('change', () => setIgnoreRenamers(cb.checked));
        parts.push(el('label', { className: 'srw-provider-filter-item', title: 'Only for this browser session' }, [cb, ' I know: continue anyway (this browser session)']));
      }
      if (!compact) parts.push(el('div', { className: 'srw-toolbar' }, [iconButton('button', 'faClipboardCheck', 'Check again', { className: 'srw-btn', onClick: load }), status]));
      box.replaceChildren(...parts);
    }
    if (!compact) box.appendChild(status);
    window.addEventListener('srw:renamer-blocked', load);
    load();
    return box;
  }

  function buildSearchActions(r, onGrabbed) {
    const wrap = el('span', { className: 'srw-search-actions' });
    getSearchCapabilities().then((caps) => {
      if (!caps || caps.error) return;
      // One joined group (v0.214.0): Now | Whisparr | Last | Manual. Dismiss sits outside of it, apart.
      const group = el('span', { className: 'srw-btn-group' });
      const status = [];
      const sources = caps.manual_sources || [];
      if (sources.length && r.entity_type && r.entity_stashdb_id) group.appendChild(buildSearchNowButton(r, onGrabbed, status));
      // Only for releases Whisparr actually tracks (2026-09-25): StashDB
      // ones, and not under the 'no_whisparr' preset -- mirrors
      // handoff.uses_whisparr().
      // Not under 'bookkeeping_only' either (v0.217.1): there Whisparr's indexers have search switched off (the sidecar searches), so its own search would
      // find nothing -- the button did nothing. It stays for the presets in which Whisparr searches.
      const whisparrTracked = r.provider === 'stashdb' && !['no_whisparr', 'bookkeeping_only'].includes(caps.scope_preset);
      if (caps.available && caps.available.whisparr && whisparrTracked) {
        const [btn, st] = buildWhisparrFallback(r);
        group.appendChild(btn);
        status.push(st);
      }
      if (r.last_search_result) group.appendChild(buildLastSearch(r, onGrabbed));
      if (sources.length) group.appendChild(buildManualSearch(r, sources, caps.backend, onGrabbed));
      // The result texts of the buttons ("nothing found", errors, the spinner) go in a line of their own: in a release row it hangs under the buttons
      // and takes no room, so the buttons stay where they are (v0.238.0); elsewhere it follows them inline.
      wrap.append(...(group.children.length ? [group] : []), el('span', { className: 'srw-search-status-line' }, status));
    });
    return wrap;
  }

  // "Now" (v0.214.0): the same automatic search the schedule runs, started for this one release right away -- whatever the back-off says,
  // because the user asked. Goes through the hand-off like the Search page's "Search now" (the release is on the wishlist already).
  // Status text with a small spinner in front: shown while a search / a hand-off runs. Assigning textContent later clears it again.
  function setBusy(node, text) {
    node.textContent = '';
    const spin = el('span', { className: 'srw-spinner' });
    spin.setAttribute('aria-hidden', 'true');
    node.append(spin, document.createTextNode(' ' + String(text).trim()));
  }

  function buildSearchNowButton(r, onDone, statusNodes) {
    const status = el('span', { className: 'srw-key-status' });
    statusNodes.push(status);
    // The magnifier, as on the movie rows (v0.243.4; a bolt before). The words are the tooltip and the aria-label.
    return iconButton('button', 'faMagnifyingGlass', 'Search now', {
      title: 'Search now: run the automatic search for this release again right now (all the usual rules: date, performers, quality)',
      onClick: async (e) => {
        const button = e.currentTarget;
        button.disabled = true;
        setBusy(status, 'searching...');
        try {
          const result = await runOp('releases_handoff', {
            provider: r.provider, entity_type: r.entity_type,
            entity_stashdb_id: r.entity_stashdb_id, scene_stashdb_id: r.scene_stashdb_id,
          });
          if (result.error) { status.textContent = ` error: ${result.error}`; notify(`Search failed: ${r.title || ''}`, result.error, 'error'); return; }
          status.textContent = result.grabbed ? ' grabbed' : (result.search_running ? ' still running' : (result.search_result ? ` ${searchStatusLabel(null, result.search_result)}` : ' done'));
          notifyHandoff(r.title, result);
          if (result.grabbed && onDone) await onDone();
        } catch (err) {
          status.textContent = ` error: ${err.message}`;
          notify(`Search failed: ${r.title || ''}`, err.message, 'error');
        } finally {
          button.disabled = false;
        }
      },
    });
  }

  function buildWhisparrFallback(r) {
    const fallbackStatus = el('span', { className: 'srw-key-status' });
    const btn = el('button', {
      className: 'srw-btn',
      textContent: 'Whisparr',
      title: 'Fall back to Whisparr\'s own automatic search+grab for this release',
      onClick: async (e) => {
        const button = e.currentTarget;
        button.disabled = true;
        fallbackStatus.textContent = ' starting...';
        try {
          const result = await runOp('releases_whisparr_fallback', {
            provider: r.provider, scene_stashdb_id: r.scene_stashdb_id,
          });
          fallbackStatus.textContent = result.error ? ` error: ${result.error}` : ' started';
          if (result.error) notify(`Whisparr fallback failed: ${r.title || ''}`, result.error, 'error');
          else notify(`Handed to Whisparr: ${r.title || ''}`, 'Whisparr searches and grabs it itself now.', 'success');
        } catch (err) {
          fallbackStatus.textContent = ` error: ${err.message}`;
          notify(`Whisparr fallback failed: ${r.title || ''}`, err.message, 'error');
        } finally {
          button.disabled = false;
        }
      },
    });
    return [btn, fallbackStatus];
  }

  const MANUAL_SOURCE_LABELS = { prowlarr: 'Prowlarr', indexers: 'Direct indexers' };
  const BACKEND_LABELS = { whisparr: 'Whisparr', ...MANUAL_SOURCE_LABELS };
  // indexer_search.py's _explain_candidates() verdicts.
  const VERDICT_LABELS = {
    grabbed: 'grabbed',
    review: 'needs review',
    lower_rank: 'OK, but another ranked higher',
    quality_not_allowed: 'quality not in profile',
    not_vr: 'not VR',
    // Stricter VR rules (2026-09-26), see indexer_search.vr_candidate_problem().
    vr_2d: '2D version of a VR scene',
    vr_no_format: 'no VR format marker (only "VR" in the name)',
    vr_res_too_high: 'above the 6K VR limit',
    vr_too_large: 'too large for one VR scene',
    too_large: 'larger than the maximum file size',
    vr_too_small: 'too small (low-quality re-encode)',
    title_mismatch: 'title/date/performers don\'t match',
    other_scene: 'already downloaded for another scene',
    blocklisted: 'previously rejected / blocklisted',
  };

  // Release title that links to the indexer's own details page when one
  // is known (2026-09-26, user request: check a possible match on the
  // indexer before accepting it). The backend never hands out URLs that
  // carry an API key (indexer_search.safe_info_url()).
  // Some indexers (MyPornClub) hand out the text of a forum post as the release name: line breaks (also as the two characters \n), web links to file
  // hosts, long runs of spaces. Shown as one clean line; the name as the indexer gave it stays in the tooltip.
  function cleanReleaseTitle(title) {
    const raw = String(title || '');
    const clean = raw.replace(/\\[nrt]/g, ' ').replace(/https?:\/\/\S+/gi, ' ').replace(/\s+/g, ' ').trim();
    return clean || raw.trim();
  }

  function releaseTitleNode(title, infoUrl, className = 'srw-review-candidate-title') {
    const shown = cleanReleaseTitle(title);
    const changed = shown !== String(title || '');
    if (!infoUrl) return el('span', { className, textContent: shown || '(unknown)', title: changed ? `As the indexer gave it: ${title}` : '' });
    return el('a', {
      className, href: infoUrl, target: '_blank', rel: 'noopener noreferrer',
      title: changed ? `Open on the indexer. As the indexer gave it: ${title}` : 'Open on the indexer', textContent: `${shown || '(unknown)'} ↗`,
    });
  }

  // Sort control for a list of search results (2026-09-26, user request; a bar of buttons since v0.238.0). Click a button to sort by it, click the
  // active one again to reverse it (the arrow shows the direction). Results without a value (no seeders, no size) always come last.
  const resolutionOf = (c) => (c.vr ? 0 : Number(c.resolution || (c.quality && c.quality.resolution) || c.est_resolution || 0));
  const RESULT_SORTS = [
    { key: 'default', label: 'Default', title: 'The order the search returned them in' },
    { key: 'age', label: 'Age', value: (c) => (c.age == null ? null : Number(c.age)), firstAsc: true, first: 'Newest first', second: 'Oldest first' },
    { key: 'size', label: 'Size', value: (c) => (c.size ? Number(c.size) : null), firstAsc: false, first: 'Largest first', second: 'Smallest first' },
    { key: 'quality', label: 'Quality', value: (c) => resolutionOf(c) || null, firstAsc: false, first: 'Highest resolution first', second: 'Lowest resolution first' },
    { key: 'seeders', label: 'Seeders', value: (c) => (c.seeders == null ? null : Number(c.seeders)), firstAsc: false, first: 'Most seeders first', second: 'Fewest seeders first', only: (items) => items.some((c) => c.seeders != null) },
    { key: 'name', label: 'Name', value: (c) => (c.title || '').toLowerCase(), firstAsc: true, first: 'A to Z', second: 'Z to A' },
  ];
  // `state` = { key, reversed }
  function sortResults(items, state) {
    const def = RESULT_SORTS.find((o) => o.key === state.key);
    const copy = [...items];
    if (!def || !def.value) return copy;
    const dirAsc = def.firstAsc !== state.reversed; // first click = the natural direction, a second click reverses it
    copy.sort((x, y) => {
      const vx = def.value(x); const vy = def.value(y);
      if (vx == null && vy == null) return 0;
      if (vx == null) return 1;
      if (vy == null) return -1;
      const d = typeof vx === 'string' ? vx.localeCompare(vy, undefined, { sensitivity: 'base' }) : vx - vy;
      return dirAsc ? d : -d;
    });
    return copy;
  }
  function buildSortBar(items, state, onChange, filterSlot = null, defaultSort = null) {
    const group = el('div', { className: 'btn-group srw-sort-bar', role: 'group', 'aria-label': 'Sort the results' });
    const draw = () => {
      group.replaceChildren(...RESULT_SORTS.filter((o) => !o.only || o.only(items)).map((o0) => {
        const o = o0.key === 'default' && defaultSort ? { ...o0, ...defaultSort } : o0; // the first button is named for what the default order is
        const active = state.key === o.key;
        const arrow = active && o.value ? (((o.firstAsc !== state.reversed)) ? ' ↑' : ' ↓') : '';
        return el('button', {
          type: 'button', className: `btn btn-secondary srw-btn-fit${active ? ' active' : ''}`, textContent: `${o.label}${arrow}`,
          title: o.value ? `${state.key === o.key ? (state.reversed ? o.second : o.first) : o.first} (click again to reverse)` : o.title,
          onClick: () => {
            if (active && o.value) state.reversed = !state.reversed; else { state.key = o.key; state.reversed = false; }
            draw();
            onChange();
          },
        });
      }));
    };
    draw();
    return el('div', { className: 'srw-sort-row' }, [el('span', { className: 'srw-sort-label', textContent: 'Sort' }), group, filterSlot]);
  }

  // The facts of a result as small chips (indexer, torrent swarm, quality, size, age); `quality` is the label already worked out by the caller.
  function resultChips(c, quality) {
    const chips = [c.indexer || 'unknown indexer'];
    if (c.protocol === 'torrent') chips.push(`torrent${c.seeders != null ? ` · ${c.seeders} seeders` : ''}${c.peers != null ? ` · ${c.peers} peers` : ''}`);
    chips.push(quality, c.size ? `${(c.size / 1e9).toFixed(2)} GB` : 'unknown size', ageWords(c.age));
    return el('span', { className: 'srw-chips' }, chips.map((t) => el('span', { className: 'srw-chip', textContent: t })));
  }

  // Search log (2026-09-25, stage 4) -- "Last search" toggles a panel
  // with what the last automatic search sent where and why each result
  // was or wasn't used. Fetched on demand (GET .../last-search), not
  // shipped with the release lists.
  // Indexer search results ("Last search", manual "Search…"): sort select
  // + text/VR filter + paging (2026-09-26 -- a search can return 100+
  // candidates).
  function buildSortedPagedResults(items, buildRow, defaultSort = null) {
    const sortState = { key: 'default', reversed: false };
    const filterSlot = el('div', { className: 'srw-sort-filter' }); // the filter bar (search + VR) sits in the sort row (v0.238.0)
    const view = buildFilteredPagedList({
      filters: ['search', 'vr'],
      perPage: 25,
      showFilterBarFrom: 1,
      barTarget: filterSlot,
      emptyText: 'No results.',
      fetchPage: localPageFetcher(() => sortResults(items, sortState), {
        text: (c) => `${c.title || ''} ${c.indexer || ''}`, vr: (c) => c.vr,
      }),
      renderItems: (page) => el('div', { className: 'srw-result-list' }, page.map(buildRow)),
    });
    view.reset();
    const children = [];
    if (items.length > 1) children.push(buildSortBar(items, sortState, () => view.reset(), filterSlot, defaultSort));
    children.push(view.node);
    return el('div', {}, children);
  }

  // One layout for every release row (v0.102.7; shared since v0.102.11): [thumbnail] [info: 1 studio/performer icons + names, 2 release name
  // (may wrap), 3 release date + badges] [buttons, never wrapped]. The rows are built from a flat list of pieces (many callers and
  // the later status decorations add to them), so the pieces are sorted into the three areas here by what they are.
  // "Open scene" for a scene that is in the library (v0.129.0): the link-button is added hidden, resolveSceneLinks() gives a whole page of
  // them their href with ONE request to the local Stash (scenes that are not in Stash keep it hidden). Used by every list that shows
  // downloaded scenes.
  function addOpenSceneLink(li, provider, sceneId) {
    if (!li || li.querySelector('a.srw-open-scene')) return null;
    const a = iconButton('a', 'faPlay', 'Open scene', { className: 'srw-btn srw-open-scene', target: '_blank', rel: 'noopener noreferrer', hidden: true,
      title: 'Open this scene in Stash (new tab)' });
    a.dataset.provider = provider || 'stashdb';
    a.dataset.scene = sceneId;
    (li.querySelector('.srw-rr-actions') || li).appendChild(a);
    return a;
  }

  // "Play in Stash" (v0.163.0): a link-button to a scene (or a film's group) of the local Stash, opened in a new tab -- the scene page plays it. EVERY row
  // where the user decides about a scene or film by hand gets one (rule of the user, 2026-10-07); `kind` is 'scene' or 'group'.
  // An icon button since v0.238.1 (like the other action columns); the words stay as tooltip and aria-label. A part number ("Play part 2") is
  // kept as text next to the icon, since the icon alone cannot tell the parts apart.
  function buildStashLink(kind, id, label) {
    const words = label || (kind === 'group' ? 'Open group' : 'Play in Stash');
    const part = /^Play part (\d+)$/.exec(words);
    const a = iconButton('a', 'faPlay', words, {
      className: 'srw-btn srw-play-link', href: `/${kind === 'group' ? 'groups' : 'scenes'}/${id}`, target: '_blank', rel: 'noopener noreferrer',
      title: kind === 'group' ? 'Open the movie\'s group in Stash (new tab)' : 'Open the scene in Stash and play it (new tab)',
    });
    if (part && a.classList.contains('srw-btn-icon')) a.append(document.createTextNode(` ${part[1]}`));
    return a;
  }

  async function resolveSceneLinks(root) {
    const links = [...root.querySelectorAll('a.srw-open-scene[hidden]')].filter((a) => !a.getAttribute('href'));
    if (!links.length) return;
    const scenes = links.map((a) => ({ provider: a.dataset.provider, scene_stashdb_id: a.dataset.scene }));
    try {
      const res = await runOp('releases_local_scenes', { scenes });
      links.forEach((a) => {
        const id = res && res.scenes && res.scenes[`${a.dataset.provider}:${a.dataset.scene}`];
        if (id) { a.href = `/scenes/${id}`; a.hidden = false; }
      });
    } catch (e) { /* no link then */ }
  }

  function arrangeReleaseRow(li, r) {
    const has = (c, ...names) => names.some((n) => c.classList.contains(n));
    const kids = [...li.children];
    const thumb = kids.filter((c) => has(c, 'srw-release-thumb-link', 'srw-release-thumb'));
    const entity = kids.filter((c) => has(c, 'srw-entity-icon-group', 'srw-local-entity-link', 'srw-local-entity-name-plain', 'srw-release-studio-context', 'srw-release-cast'));
    const title = kids.filter((c) => c.classList.contains('srw-release-title'));
    // Buttons AND link-buttons ("Open scene", "Open group", "Open file in Stash") are actions; the Scene / Movie pill sits next to the title (v0.128.5).
    const actions = kids.filter((c) => c.tagName === 'BUTTON' || (c.tagName === 'A' && c.classList.contains('srw-btn'))
      || has(c, 'srw-search-actions', 'srw-inline-confirm', 'srw-btn-group'));
    // Result / progress texts of the buttons ("searching...", "Still no match ...") get a line of their own under the row's text: in the buttons'
    // column they widened it and shifted the whole table (v0.235.1).
    const statuses = kids.filter((c) => has(c, 'srw-key-status'));
    const pills = kids.filter((c) => c.classList.contains('srw-film-class'));
    const wide = kids.filter((c) => has(c, 'srw-review-candidate'));
    const used = new Set([...thumb, ...entity, ...title, ...pills, ...actions, ...statuses, ...wide]);
    const meta = kids.filter((c) => !used.has(c));
    // The release date comes first in the third line; a date span that just repeats it is dropped.
    const sceneDate = (r && (r.scene_date || (r.scene_created || '').slice(0, 10))) || '';
    const metaClean = meta.filter((c) => !(c.classList.contains('srw-release-date') && c.textContent.trim() === sceneDate));
    // Imported although the fingerprints did not match -- the user confirmed the file (v0.106.6, file_importer.confirm_mismatched_file()).
    if (r && r.import_error_code === 'user_confirmed') {
      metaClean.push(el('span', { className: 'srw-confirmed-badge', textContent: 'confirmed by you',
        title: r.import_error_detail || 'Imported although the file matches none of the provider\'s fingerprints -- you confirmed it' }));
    }
    if (sceneDate) metaClean.unshift(el('span', { className: 'srw-release-date srw-release-scene-date', title: 'Release date', textContent: sceneDate }));
    li.replaceChildren();
    li.classList.add('srw-release-row');
    if (!thumb.length) li.classList.add('srw-release-row-nothumb');
    thumb.forEach((c) => li.appendChild(c));
    li.appendChild(el('div', { className: 'srw-rr-info' }, [
      el('div', { className: 'srw-rr-entity' }, entity),
      el('div', { className: 'srw-rr-title' }, [...title, ...pills]),
      el('div', { className: 'srw-rr-meta' }, metaClean),
      el('div', { className: 'srw-rr-status' }, statuses),
    ]));
    li.appendChild(el('div', { className: 'srw-rr-actions' }, actions));
    wide.forEach((c) => li.appendChild(c));
    return li;
  }

  // "Whisparr's file doesn't match this scene (found in your library as "<title>")": the title links to that scene in Stash (v0.121.0), so a
  // mislabelled or duplicate file can be looked at at once. info = a library_status entry.
  function mismatchWarning(info) {
    const found = info.mismatch_scene_id
      ? el('a', { href: `/scenes/${info.mismatch_scene_id}`, target: '_blank', rel: 'noopener noreferrer', textContent: info.mismatch_title || 'scene',
        title: 'Open that scene in Stash (new tab)' })
      : document.createTextNode(`"${info.mismatch_title}"`);
    return el('span', { className: 'srw-release-mismatch-warning' }, [
      "Whisparr's file doesn't match this scene (found in your library as ", found, ') -- likely a mismatched release.']);
  }

  // Where later decorations put a badge / note: the third line of the row.
  const rowMeta = (li) => li.querySelector('.srw-rr-meta') || li;

  // A dialog in Stash's own modal markup (Bootstrap: .modal / .modal-dialog / .modal-content), so a theme restyles it like Stash's own dialogs
  // (v0.238.0). `title` is the heading, `node` the content; Escape, the x and a click beside the dialog close it. Returns {close, body}.
  function openModal(title, node, { onClose = null, size = 'modal-xl', subtitle = null } = {}) {
    applyThemeColors();
    const backdrop = el('div', { className: 'modal-backdrop fade show srw-modal-backdrop' });
    const closeBtn = el('button', { type: 'button', className: 'close', 'aria-label': 'Close' }, [el('span', { 'aria-hidden': 'true', textContent: '×' })]);
    const body = el('div', { className: 'modal-body srw-modal-body' }, [node]);
    const dialog = el('div', { className: `modal-dialog ${size} modal-dialog-scrollable modal-dialog-centered`, role: 'document' }, [
      el('div', { className: 'modal-content' }, [
        el('div', { className: 'modal-header' }, [el('div', { className: 'srw-modal-heading' }, [el('h5', { className: 'modal-title srw-modal-title', textContent: title }), subtitle]), closeBtn]),
        body,
      ]),
    ]);
    const modal = el('div', { className: 'modal fade show srw-modal', tabindex: '-1', role: 'dialog', 'aria-modal': 'true' }, [dialog]);
    modal.style.display = 'block';
    let closed = false;
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    function close() {
      if (closed) return;
      closed = true;
      document.removeEventListener('keydown', onKey);
      modal.remove();
      backdrop.remove();
      if (!document.querySelector('.srw-modal')) document.body.classList.remove('modal-open');
      if (onClose) onClose();
    }
    closeBtn.addEventListener('click', close);
    modal.addEventListener('mousedown', (e) => { if (e.target === modal) close(); });
    document.addEventListener('keydown', onKey);
    document.body.classList.add('modal-open');
    document.body.append(backdrop, modal);
    // The dialog's own surface color (the theme's), for things that stick to the top of its scrolling body and need an opaque ground (the player bar).
    try { modal.style.setProperty('--srw-surface', getComputedStyle(dialog.querySelector('.modal-content')).backgroundColor); } catch (e) { /* the stylesheet's fallback */ }
    return { close, body };
  }

  // The scene a search modal is about, under its heading: title, then performers, studio and date (v0.238.0) -- the modal covers the row, and these
  // are the words one builds a manual query from.
  function sceneSubtitle(r) {
    const people = Array.isArray(r.cast) && r.cast.length ? r.cast.map((p) => p.name)
      : String(r.cast_names || '').split('|').filter(Boolean);
    if (!people.length && r.entity_type === 'performer' && r.entity_name) people.push(r.entity_name);
    const meta = [people.join(', '), r.studio_name, r.scene_date].filter(Boolean).join(' · ');
    return el('div', { className: 'srw-modal-sub' }, [
      el('div', { className: 'srw-modal-sub-title', textContent: r.title || '' }),
      meta ? el('div', { className: 'srw-modal-sub-meta', textContent: meta }) : null,
    ]);
  }

  // Open / close of a result panel (Last search, manual search): the panel is shown in a modal (v0.238.0; before, it was pushed in under the row
  // and moved the list). The buttons keep their text. Returns {isOpen, current, open(node), close()}; `title` is the modal's heading.
  function panelToggle(toggleBtn, attach, title = '', subtitle = null) {
    let panel = null;
    let modal = null;
    const close = () => { const m = modal; modal = null; panel = null; if (m) m.close(); };
    return {
      get isOpen() { return Boolean(panel); },
      get current() { return panel; },
      close,
      open(node) {
        panel = node;
        modal = openModal(title, node, { subtitle, onClose: () => { if (panel === node) { panel = null; modal = null; } } });
      },
    };
  }

  // Runs `fn` now -- or, while a search panel ("Search…" / "Last search") is open inside `root`, as soon as it is closed (checked every 2 s, once).
  // For live reloads: rebuilding the list would otherwise close the panel and throw the results away while the user reads them.
  function whenNoOpenPanel(root, fn) {
    const isOpen = () => Boolean(root.querySelector('.srw-manual-search-panel'));
    if (!isOpen()) { fn(); return; }
    if (root.srwWaitingForPanel) return;
    root.srwWaitingForPanel = true;
    const timer = setInterval(() => {
      if (root.isConnected && isOpen()) return;
      clearInterval(timer);
      root.srwWaitingForPanel = false;
      if (root.isConnected) fn();
    }, 2000);
  }

  function buildLastSearch(r, onGrabbed) {
    const wrap = el('span', { className: 'srw-prowlarr-search' });
    // The history clock (v0.241.4), the icon of the History page.
    const toggleBtn = iconButton('button', ['faRotateLeft', 'faMagnifyingGlass'], 'Last search', { title: 'Last search: what the last automatic search asked for, and why nothing was taken' });
    const toggle = panelToggle(toggleBtn, null, 'Last search', sceneSubtitle(r));
    let panel = null;
    toggleBtn.addEventListener('click', async () => {
      if (toggle.isOpen) { toggle.close(); panel = null; return; }
      const loading = el('span', { className: 'srw-hint' });
      setBusy(loading, 'Loading...');
      panel = el('div', { className: 'srw-manual-search-panel' }, [loading]);
      toggle.open(panel);
      const mine = panel;
      const log = await runOp('releases_last_search', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id });
      if (toggle.current !== mine) return; // closed (or closed and opened again) while it was loading
      panel = mine;
      panel.innerHTML = '';
      if (log.error) {
        panel.appendChild(el('p', {
          className: 'srw-hint',
          textContent: /404/.test(log.error)
            ? 'No details logged for this release yet — only searches from v0.58.0 on record them.'
            : `Error: ${log.error}`,
        }));
        return;
      }
      const where = BACKEND_LABELS[log.backend] || log.backend;
      const when = log.searched_at ? log.searched_at.slice(0, 16).replace('T', ' ') : '';
      const shown = (log.candidates || []).length;
      // `queries` since v0.59.0 (staged title -> performers -> studio+date
      // queries, see indexer_search._build_queries()); v0.58.0 logs had
      // a single `query`; Whisparr builds its own, so neither is set.
      const queries = log.queries || (log.query ? [{ query: log.query, results: log.total }] : []);
      const what = queries.length
        ? queries.map((q) => `"${q.query}" (${q.error ? `failed: ${q.error}` : `${q.results} result(s)`})`).join(', then ')
        : 'its own query (built from the scene)';
      panel.appendChild(el('p', {
        className: 'srw-hint',
        textContent: `${when} — searched ${where} for ${what}. ${log.total} distinct result(s)`
          + `${shown < log.total ? `, showing ${shown}` : ''}.`,
      }));
      if (log.error) panel.appendChild(el('p', { className: 'srw-hint', textContent: `Error: ${log.error}` }));
      if (log.kept_from) { // the last search failed (the indexers did not answer): the results are those of an older one
        panel.appendChild(el('p', { className: 'srw-review-warning', textContent: `The search failed; the results below are from the search of ${log.kept_from.slice(0, 16)} UTC.` }));
      }
      panel.appendChild(buildSortedPagedResults(log.candidates || [], buildLogRow, {
        label: 'Best match',
        title: 'The search\'s own verdict, best first: taken, needs review, another ranked higher, quality or size not allowed, title/date/performers do not match, other scene, blocklisted',
      }));
    });
    wrap.appendChild(toggleBtn);
    return wrap;

    function buildLogRow(c) {
        const qualityLabel = c.vr ? 'VR' : (c.resolution ? `${c.resolution}p` : 'unknown quality');
        const sizeGb = c.size ? `${(c.size / 1e9).toFixed(2)} GB` : 'unknown size';
        const swarm = c.protocol === 'torrent'
          ? ` (torrent${c.seeders != null ? `, ${c.seeders} seeders` : ''}${c.peers != null ? `, ${c.peers} peers` : ''})` : '';
        const quality = c.vr ? 'VR' : (c.resolution ? `${c.resolution}p` : (c.est_resolution ? `~${c.est_resolution}p (estimated)` : 'unknown quality'));
        const rowStatus = el('span', { className: 'srw-key-status' });
        // Grab = this result instead of what is running (a running torrent is removed with its data), same as the Manual search's Grab
        const grab = el('button', {
          className: 'srw-btn srw-btn-primary', textContent: 'Grab',
          title: 'Download this result now. A torrent that is running for this scene is removed with its data and not used again.',
          onClick: (e) => confirmInline(e.currentTarget, `Download "${cleanReleaseTitle(c.title)}" instead? A running torrent of this scene is removed with its data.`, async () => {
            setBusy(rowStatus, 'searching the indexers again and grabbing...');
            try {
              const result = await runOp('releases_last_search_grab', { provider: r.provider, scene_stashdb_id: r.scene_stashdb_id, candidate: c });
              if (result.error) { rowStatus.textContent = ` error: ${result.error}`; notify(`Grab failed: ${r.title || ''}`, result.error, 'error'); return; }
              if (!result.grab) { rowStatus.textContent = ` not grabbed: ${String(result.result || 'refused').replace(/_/g, ' ')}`; return; }
              rowStatus.textContent = ' grabbed';
              notify(`Grabbed: ${r.title || ''}`, `${c.title} (${c.indexer || 'indexer'}) -- sent to the download client.`, 'success');
              if (onGrabbed) await onGrabbed();
            } catch (err) { rowStatus.textContent = ` error: ${err.message}`; }
          }),
        });
        return el('div', { className: `srw-manual-search-result srw-result-row srw-verdict-${c.verdict}` }, [
          el('div', { className: 'srw-result-main' }, [
            el('div', { className: 'srw-result-head' }, [
              el('span', { className: 'srw-verdict', textContent: VERDICT_LABELS[c.verdict] || c.verdict }),
              releaseTitleNode(c.title, c.info_url),
            ]),
            el('div', { className: 'srw-result-facts' }, [
              resultChips(c, quality),
              ageWarning(c.age, r.scene_date),
              c.size_note ? el('span', { className: 'srw-review-warning', textContent: 'size?', title: c.size_note }) : null,
            ]),
          ]),
          el('div', { className: 'srw-result-actions' }, [grab, rowStatus]),
        ]);
    }
  }

  // Results are shown exactly as the backend returns them -- unfiltered,
  // no automatic plausibility gate (see routers/releases.py's
  // manual_search() docstring) -- the human reading the full title is
  // the filter here.
  // Prefill of the manual search (v0.103.0): "Studio.YY.MM.DD" -- the form most release names start with ("AnalIntroductions.26.09.22
  // .Jadilica..."); the scene title only when studio or date are missing.
  function manualSearchPrefill(r) {
    const studio = String(r.studio_name || '').replace(/[^A-Za-z0-9]/g, '');
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(r.scene_date || '');
    return studio && m ? `${studio}.${m[1].slice(2)}.${m[2]}.${m[3]}` : (r.title || '');
  }

  // `opts` (v0.106.0): {grab: async (candidate) => result -- replaces the default grab, host: () => element the result panel goes into}
  // -- Maintenance > Upgrade wanted uses it for an upgrade grab (records upgrade_of) in its own row layout.
  function buildManualSearch(r, sources, backend, onGrabbed, opts = null) {
    const wrap = el('span', { className: 'srw-prowlarr-search' });
    // A pencil with a magnifier (v0.241.3): your own query. The words are the tooltip and the aria-label.
    const toggleBtn = iconButton('button', ['faPencil', 'faMagnifyingGlass'], 'Manual search', {
      title: 'Manual search: search the indexers yourself with your own query and grab a result',
    });
    const toggle = panelToggle(toggleBtn, null, 'Manual search', sceneSubtitle(r));
    let panel = null;
    toggleBtn.addEventListener('click', () => {
      if (toggle.isOpen) { toggle.close(); panel = null; return; }

      const input = el('input', { type: 'search', className: 'srw-search-input', value: manualSearchPrefill(r) });
      let sourceSelect = null;
      if (sources.length > 1) {
        sourceSelect = el('select', { className: 'srw-filter-preset-select' },
          sources.map((s) => el('option', { value: s, textContent: MANUAL_SOURCE_LABELS[s] || s })));
        sourceSelect.value = sources.includes(backend) ? backend : sources[0];
      }
      const goBtn = el('button', { className: 'srw-btn srw-btn-sm', textContent: 'Go' });
      const status = el('span', { className: 'srw-key-status' });
      const resultsWrap = el('div');

      function buildResultRow(candidate) {
        const sizeGb = candidate.size ? `${(candidate.size / 1e9).toFixed(2)} GB` : 'unknown size';
        const quality = (candidate.quality && candidate.quality.quality) || {};
        // a name without a resolution: the sidecar judges one from the scene's length and the size (est_resolution), marked with "~"
        const qualityLabel = quality.source === 'vr' ? 'VR' : (quality.resolution ? `${quality.resolution}p`
          : (candidate.est_resolution ? `~${candidate.est_resolution}p (estimated)` : 'unknown quality'));
        const rowStatus = el('span', { className: 'srw-key-status' });
        // One grab button: `grab` = the caller's own grab (null = the default grab of this panel), `ask` = the confirm text
        const grabButton = (label, className, grab, ask, hint) => el('button', {
          className, textContent: label, title: hint || '',
          onClick: (e) => {
            confirmInline(e.currentTarget, candidate.tried ? `${ask} (Note: ${candidate.tried}.)` : ask, async () => {
              rowStatus.textContent = ' starting...';
              try {
                const result = grab ? await grab(candidate) : (opts && opts.grab ? await opts.grab(candidate) : await runOp('releases_manual_search_grab', {
                  provider: r.provider, scene_stashdb_id: r.scene_stashdb_id, candidate,
                }));
                if (result.error) {
                  rowStatus.textContent = ` error: ${result.error}`;
                  notify(`Grab failed: ${r.title || ''}`, result.error, 'error');
                  return;
                }
                if (result.result && !result.grab) { // the sidecar did not send it (e.g. another download of the scene is running)
                  rowStatus.textContent = ` not grabbed: ${String(result.result).replace(/_/g, ' ')}`;
                  return;
                }
                rowStatus.textContent = ' grabbed';
                notify(`Grabbed: ${r.title || ''}`, `${candidate.title} (${candidate.indexer || 'indexer'}) -- sent to the download client.`, 'success');
                if (onGrabbed) await onGrabbed();
              } catch (err) {
                rowStatus.textContent = ` error: ${err.message}`;
              }
            });
          },
        });
        return el('div', { className: 'srw-manual-search-result srw-result-row' }, [
          el('div', { className: 'srw-result-main' }, [
            el('div', { className: 'srw-result-head' }, [releaseTitleNode(candidate.title, candidate.infoUrl)]),
            el('div', { className: 'srw-result-facts' }, [
              resultChips(candidate, qualityLabel),
              ageWarning(candidate.age, r.scene_date),
              candidate.size_note ? el('span', { className: 'srw-review-warning', textContent: 'size?', title: candidate.size_note }) : null,
              candidate.tried ? el('span', { className: 'srw-review-warning', textContent: candidate.tried.startsWith('declined') ? 'declined before' : 'tried before',
                title: `${candidate.tried} (blocklist of this scene)` }) : null,
            ]),
          ]),
          el('div', { className: 'srw-result-actions' }, [
            grabButton('Grab', 'srw-btn srw-btn-primary', null, `Download "${candidate.title}" for "${r.title}"?`),
            opts && opts.grabAsScene ? grabButton('Grab as one scene', 'srw-btn', opts.grabAsScene,
              `Download "${candidate.title}" as ONE SCENE of "${r.title}"? It joins the movie's group; the movie stays on the wishlist for the other parts.`,
              'The file is only a part of the movie: it is added to its group and the movie stays wanted for the other parts') : null,
            rowStatus,
          ]),
        ]);
      }

      async function runSearch() {
        const query = input.value.trim();
        if (!query) return;
        setBusy(status, 'searching...');
        resultsWrap.innerHTML = '';
        try {
          const response = await runOp('releases_manual_search', {
            provider: r.provider, scene_stashdb_id: r.scene_stashdb_id, query,
            source: sourceSelect ? sourceSelect.value : sources[0],
          });
          if (response.error) { status.textContent = ` error: ${response.error}`; return; }
          status.textContent = '';
          const results = response.results || [];
          if (results.length === 0) {
            resultsWrap.appendChild(el('p', { className: 'srw-hint', textContent: 'No results.' }));
            return;
          }
          resultsWrap.appendChild(buildSortedPagedResults(results, buildResultRow, {
            label: 'Indexer order',
            title: 'As the search returned them: by the priority of the indexer (lowest number first), then more seeders, then newer, then larger',
          }));
        } catch (err) {
          status.textContent = ` error: ${err.message}`;
        }
      }

      goBtn.addEventListener('click', runSearch);
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') runSearch(); });
      panel = el('div', { className: 'srw-manual-search-panel' }, [
        el('span', {}, [input, sourceSelect, goBtn, status]),
        resultsWrap,
      ]);
      toggle.open(panel);
    });
    wrap.appendChild(toggleBtn);
    return wrap;
  }

  // ==================== Download + import progress (2026-09-26) ====================
  //
  // One progress bar per grabbed release, fed by releases_download_progress
  // (routers/releases.py's download_progress()), now covering the whole
  // path: downloading -> waiting for import -> importing (file copy, with
  // % / speed / time left) -> Stash scan + verification -> Imported.
  // Shared by the Activity page and the Releases page's "On Wishlist".
  function formatBytes(n) {
    if (n == null) return '?';
    if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
    return `${Math.round(n / 1e6)} MB`;
  }

  function formatSeconds(sec) {
    if (sec == null) return '';
    const m = Math.floor(sec / 60);
    const h = Math.floor(m / 60);
    return h ? `${h}:${String(m % 60).padStart(2, '0')}:${String(sec % 60).padStart(2, '0')}`
      : `${m}:${String(sec % 60).padStart(2, '0')}`;
  }

  // "2 min", "5 h", "2 d 4 h" since an epoch second
  function formatWaited(sinceEpoch) {
    const sec = Math.max(0, Math.floor(Date.now() / 1000) - sinceEpoch);
    if (sec < 90) return 'under 2 min';
    if (sec < 3600) return `${Math.round(sec / 60)} min`;
    if (sec < 86400) return `${Math.floor(sec / 3600)} h`;
    return `${Math.floor(sec / 86400)} d ${Math.floor((sec % 86400) / 3600)} h`;
  }

  function describeProgress(p) {
    if (!p) return null;
    switch (p.phase) {
      case 'waiting_import':
        return { pct: 100, text: 'Download complete — waiting for import' };
      case 'preparing':
        return { pct: 0, text: 'Import starting…' };
      case 'waiting_encode': {
        // (v0.232.1) A download that waits for the re-encode must not look like a stuck import: it says who is expected to encode and,
        // when the encode worker (another machine) is offline, that it has to be started -- in amber, with how long it has waited.
        const waited = p.queued_at ? ` — waiting ${formatWaited(p.queued_at)}` : '';
        if (!p.can_local) return { pct: 100, text: `Download complete — queued for the re-encode to 1080p on the NAS${waited}` };
        if (p.worker_online === undefined) return { pct: 100, text: `Download complete — waiting for the encode worker${waited}` }; // a sidecar that does not report it yet
        if (p.worker_online) {
          return { pct: 100, text: `Download complete — waiting for the encode worker${p.worker_name ? ` (${p.worker_name}, online)` : ''}, it takes the job next${waited}` };
        }
        return { pct: 100, warn: true, text: `⚠ Waiting for the re-encode — the encode worker is OFFLINE: start it on your PC (SRM Encode Worker), or encode on the NAS${waited}` };
      }
      case 'encoding': {
        const left = p.seconds_left != null ? `, ${formatSeconds(p.seconds_left)} left` : '';
        const speed = p.speed_x ? `, ${p.speed_x}x` : '';
        const how = p.method ? ` (${p.method})` : '';
        return { pct: p.percentage || 0, text: `Re-encoding to 1080p${how} — ${p.percentage || 0}%${speed}${left}` };
      }
      case 'copying': {
        const left = p.seconds_left != null ? `, ${formatSeconds(p.seconds_left)} left` : '';
        const speed = p.speed_bps ? `, ${formatBytes(p.speed_bps)}/s` : '';
        return {
          pct: p.percentage || 0,
          text: `Importing into the library — ${p.percentage || 0}% (${formatBytes(p.bytes_done)} of `
            + `${formatBytes(p.bytes_total)}${speed}${left})`,
        };
      }
      case 'verifying':
        return { pct: 100, text: 'In the library — Stash scan & verification pending' };
      case 'torrent_problem': // the torrent monitor (v0.231.0): in error / gone from the download client
        return { pct: 0, text: `Torrent problem — ${p.health_detail || p.health || ''}` };
      default: {
        const pct = p.percentage != null ? Number(p.percentage) : 0;
        const timeleft = p.timeleft && p.timeleft !== '0:00:00' ? `, ${p.timeleft} left` : '';
        const swarm = p.seeds != null || p.peers != null ? `, ${p.seeds ?? 0} seeds / ${p.peers ?? 0} peers` : '';
        const note = p.health_detail ? ` — ${p.health_detail}` : '';
        return { pct, text: `${p.status || 'Downloading'} — ${pct}%${timeleft}${p.health ? swarm : ''}${note}` };
      }
    }
  }

  // Updates one registered bar ({fill, text, wrap?}); returns whether it
  // is still active (anything but "no data").
  function applyProgress(elements, p) {
    const d = describeProgress(p);
    const phase = (p && p.phase) || 'none';
    if (elements.wrap) elements.wrap.dataset.phase = phase;
    if (elements.wrap) elements.wrap.dataset.warn = d && d.warn ? '1' : ''; // amber: needs the user (a worker to start)
    if (elements.localBtn) elements.localBtn.style.display = phase === 'waiting_encode' && p.can_local ? '' : 'none'; // "Encode on the NAS now"
    if (!d) {
      elements.fill.style.width = '0%';
      elements.text.textContent = '';
      return false;
    }
    elements.fill.style.width = `${Math.max(0, Math.min(100, d.pct))}%`;
    elements.text.textContent = d.text;
    return true;
  }

  // ==================== Filtered + paged lists (2026-09-26) ====================
  //
  // User principle: every list that could otherwise grow without bound
  // is paged, and paging always comes with our usual filter bar (same
  // look/controls as the List view). One component for all of them:
  //
  //   const view = buildFilteredPagedList({
  //     filters: ['search', 'description', 'type', 'vr', 'dates', 'providers', 'late'],
  //     fetchPage: async (f, page, perPage) => ({ total, items }),  // server- or client-side
  //     renderItems: (items) => Node,                               // one page
  //     perPage: 40, emptyText, filteredEmptyText, onTotal: (total, filterActive) => {},
  //   });
  //   parent.appendChild(view.node); view.reload(); view.setSearch('...');
  //
  // `f` is the filter state: {search, description, type ('both'|'studio'|
  // 'performer'), vr ('all'|'vr'|'non_vr'), dateFrom, dateUntil,
  // providers {stashdb, theporndb}, late ('all'|'only'|'hide')}.
  // releaseQueryArgs(f) maps it to releases_query arguments;
  // filterLocally(items, f, fields) applies it in the browser.
  // Scene pairing (v0.182.0): release.cast_pairing -- 'B/G', 'G/G' ... ; '' = the provider lists no cast (filter key PAIRING_NONE).
  const PAIRING_NONE = '(none)';
  // [{key, label, count}] for the filter's drop-down, most frequent first, from the rows' cast_pairing (null = not read: not counted).
  function pairingOptionsFor(items) {
    const counts = new Map();
    items.forEach((r) => { if (r.cast_pairing != null) counts.set(r.cast_pairing, (counts.get(r.cast_pairing) || 0) + 1); });
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([code, count]) => ({ key: code === '' ? PAIRING_NONE : code, label: code === '' ? 'No cast listed' : code, count }));
  }

  // [{name, count}] of the performers in these scenes (cast_names '|A|B|'), most scenes first; `exclude` = the page's own performer.
  function castOptionsFor(items, exclude) {
    const skip = String(exclude || '').trim().toLowerCase();
    const counts = new Map();
    items.forEach((r) => String(r.cast_names || '').split('|').forEach((n) => {
      if (n && n.toLowerCase() !== skip) counts.set(n, (counts.get(n) || 0) + 1);
    }));
    return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([name, count]) => ({ name, count }));
  }

  function buildPairingBadge(r) {
    if (!r || !r.cast_pairing) return null;
    return el('span', { className: 'srw-late-badge', title: `Scene pairing by the cast's genders (B male, G female, T trans/non-binary, ? unknown)${r.cast_names ? ` -- with ${r.cast_names.split('|').filter(Boolean).join(', ')}` : ''}`, textContent: r.cast_pairing });
  }

  function defaultFilterState() {
    return {
      search: '', description: false, tags: false, type: 'both', vr: 'all', kind: 'all', dateFrom: '', dateUntil: '',
      providers: { stashdb: true, theporndb: true }, late: 'all', upgrades: 'all', pairing: '', pairingOptions: [], cast: '', castOptions: [],
    };
  }

  function isFilterActive(f) {
    return Boolean(f.search.trim() || f.type !== 'both' || f.vr !== 'all' || f.kind !== 'all' || f.dateFrom || f.dateUntil
      || !providersAllOn(f.providers) || f.late !== 'all' || f.upgrades !== 'all' || Boolean(f.pairing) || Boolean(f.cast.trim()));
  }

  function releaseQueryArgs(f) {
    return {
      q: f.search.trim(),
      search_description: f.description,
      search_tags: f.tags,
      entity_type: f.type === 'both' ? null : f.type,
      vr: f.vr === 'all' ? null : f.vr,
      providers: providersSelected(f.providers),
      date_from: f.dateFrom || null,
      date_until: f.dateUntil || null,
      late_addition: f.late === 'all' ? null : f.late === 'only',
      upgrades: f.upgrades === 'all' ? null : f.upgrades,
    };
  }

  // Browser-side version for lists that are already fully loaded.
  // `fields` maps filter names to accessors, e.g. {text: (x) => x.name,
  // type: (x) => x.entity_type, date: (x) => x.newest_scene_date}; a
  // filter without an accessor is ignored.
  function filterLocally(items, f, fields) {
    const q = f.search.trim().toLowerCase();
    return items.filter((x) => {
      if (q && fields.text) {
        const text = `${fields.text(x) || ''} ${f.description && fields.description ? fields.description(x) || '' : ''}`;
        if (!text.toLowerCase().includes(q)) return false;
      }
      if (f.type !== 'both' && fields.type && fields.type(x) !== f.type) return false;
      if (f.kind !== 'all' && fields.kind && fields.kind(x) !== f.kind) return false;  // 'movies' | 'scenes' (lists that hold both)
      if (f.vr !== 'all' && fields.vr && Boolean(fields.vr(x)) !== (f.vr === 'vr')) return false;
      if (fields.provider) {
        const p = fields.provider(x);
        if (p in f.providers && !f.providers[p]) return false;
      }
      if ((f.dateFrom || f.dateUntil) && fields.date) {
        const d = (fields.date(x) || '').slice(0, 10);
        if (f.dateFrom && (!d || d < f.dateFrom)) return false;
        if (f.dateUntil && (!d || d > f.dateUntil)) return false;
      }
      if (f.late !== 'all' && fields.late && Boolean(fields.late(x)) !== (f.late === 'only')) return false;
      if (f.upgrades !== 'all' && fields.upgrade && Boolean(fields.upgrade(x)) !== (f.upgrades === 'only')) return false;
      if (f.cast.trim() && fields.cast && !(fields.cast(x) || '').toLowerCase().includes(f.cast.trim().toLowerCase())) return false;
      if (f.pairing && fields.pairing && (fields.pairing(x) || '') !== (f.pairing === PAIRING_NONE ? '' : f.pairing)) return false;
      return true;
    });
  }

  function buildToggleGroup(options, getValue, setValue) {
    const buttons = options.map(({ key, label, title }) => el('button', {
      className: 'srw-btn srw-btn-sm', textContent: label, title: title || '',
      onClick: () => { setValue(key); sync(); },
    }));
    function sync() {
      buttons.forEach((btn, i) => btn.classList.toggle('srw-btn-primary', options[i].key === getValue()));
    }
    sync();
    return el('span', { className: 'srw-view-toggle' }, buttons);
  }

  // A drop-down for a filter with a few values (v0.225.5; was a row of buttons): one small control per filter, all on the bar's row.
  function buildSelect(options, getValue, setValue, title) {
    const sel = el('select', { className: 'srw-filter-select srw-provider-filter-item', title: title || '' },
      options.map(({ key, label }) => el('option', { value: key, textContent: label })));
    sel.value = getValue();
    sel.addEventListener('change', () => setValue(sel.value));
    return sel;
  }

  function buildPagedFilterBar(state, filters, onChange) {
    const parts = [];
    let debounce = null;
    if (filters.includes('search')) {
      parts.push(el('input', {
        type: 'search', placeholder: filters.includes('description') ? 'Search title or studio…' : 'Search…',
        value: state.search, className: 'srw-search-input',
        oninput: (e) => {
          state.search = e.target.value;
          clearTimeout(debounce);
          debounce = setTimeout(onChange, 300);
        },
      }));
    }
    if (filters.includes('description')) {
      const cb = el('input', { type: 'checkbox' });
      cb.checked = state.description;
      cb.addEventListener('change', () => { state.description = cb.checked; onChange(); });
      parts.push(el('label', {
        className: 'srw-provider-filter-item', title: 'Also match the scene description, not only title/studio',
      }, [cb, ' incl. description']));
      const tagCb = el('input', { type: 'checkbox' });
      tagCb.checked = Boolean(state.tags);
      tagCb.addEventListener('change', () => { state.tags = tagCb.checked; onChange(); });
      parts.push(el('label', {
        className: 'srw-provider-filter-item', title: 'Also match the scene\'s tags (e.g. "anal", "MILF") -- for videos whose title does not say it',
      }, [tagCb, ' incl. tags']));
    }
    if (filters.includes('kind')) {
      parts.push(buildSelect(
        [{ key: 'all', label: 'All releases' }, { key: 'movies', label: 'Movies' }, { key: 'scenes', label: 'Scenes' }],
        () => state.kind, (v) => { state.kind = v; onChange(); }, 'Scenes, movies or both (lists that hold both)'));
    }
    if (filters.includes('type')) {
      parts.push(buildSelect(
        [{ key: 'both', label: 'All types' }, { key: 'studio', label: 'Studios' }, { key: 'performer', label: 'Performer' }],
        () => state.type, (v) => { state.type = v; onChange(); }, 'Came in through a watched studio or a watched performer'));
    }
    if (filters.includes('vr')) {
      parts.push(buildSelect(
        [{ key: 'all', label: 'VR + Non-VR' }, { key: 'vr', label: 'VR' }, { key: 'non_vr', label: 'Non-VR' }],
        () => state.vr, (v) => { state.vr = v; onChange(); }, 'VR or flat releases'));
    }
    if (filters.includes('upgrades')) {
      parts.push(buildSelect(
        [{ key: 'all', label: 'With upgrades' }, { key: 'hide', label: 'Hide upgrades' }, { key: 'only', label: 'Only upgrades' }],
        () => state.upgrades, (v2) => { state.upgrades = v2; onChange(); }, 'Upgrades replace a file that is already in your library (re-adds, better versions)'));
    }
    if (filters.includes('late')) {
      parts.push(buildSelect(
        [{ key: 'all', label: 'All additions' }, { key: 'only', label: 'Added late' }, { key: 'hide', label: 'Not added late' }],
        () => state.late, (v) => { state.late = v; onChange(); }, 'Back-catalogue additions found by the scan'));
    }
    if (filters.includes('cast')) {
      // Co-performer filter (v0.182.0): type a name (matches any part of it) or pick one from the list of the performers in these scenes.
      const listId = `srw-cast-list-${Math.random().toString(36).slice(2, 8)}`;
      const input = el('input', {
        type: 'search', className: 'srw-search-input', placeholder: 'With performer…',
        title: 'Only scenes that also have this performer: type a name or pick one from the list (name with the number of scenes)',
      });
      input.setAttribute('list', listId);
      input.value = state.cast;
      const dl = el('datalist', { id: listId }, state.castOptions.map((o) => el('option', { value: o.name, label: `${o.name} (${o.count})` })));
      let t = null;
      input.addEventListener('input', () => { clearTimeout(t); t = setTimeout(() => { state.cast = input.value; onChange(); }, 250); });
      parts.push(el('span', {}, [input, dl]));
    }
    if (filters.includes('pairing') && state.pairingOptions.length) {
      const sel = el('select', {
        className: 'srw-provider-filter-item',
        title: 'Scene pairing by the cast\'s genders: B = male, G = female, T = trans / non-binary / intersex, ? = no gender given. Five or more performers = Group.',
      }, [el('option', { value: '', textContent: 'All pairings' }),
        ...state.pairingOptions.map((o) => el('option', { value: o.key, textContent: `${o.label} (${o.count})` }))]);
      sel.value = state.pairing;
      sel.addEventListener('change', () => { state.pairing = sel.value; onChange(); });
      parts.push(sel);
    }
    if (filters.includes('dates')) {
      const from = el('input', { type: 'date', className: 'srw-list-date-filter', value: state.dateFrom });
      const until = el('input', { type: 'date', className: 'srw-list-date-filter', value: state.dateUntil });
      from.addEventListener('change', () => { state.dateFrom = from.value; onChange(); });
      until.addEventListener('change', () => { state.dateUntil = until.value; onChange(); });
      parts.push(el('span', { className: 'srw-list-date-range' }, [
        el('span', { className: 'srw-list-date-range-label', textContent: 'from' }), from,
        el('span', { className: 'srw-list-date-range-label', textContent: 'until' }), until,
        el('button', {
          className: 'srw-btn srw-btn-sm', textContent: '×', title: 'Clear date filter',
          onClick: () => { state.dateFrom = ''; state.dateUntil = ''; from.value = ''; until.value = ''; onChange(); },
        }),
      ]));
    }
    if (filters.includes('providers')) {
      const boxes = providerKeys().map((key) => {
        const cb = el('input', { type: 'checkbox' });
        cb.checked = providerOn(state.providers, key);
        cb.addEventListener('change', () => { state.providers[key] = cb.checked; onChange(); });
        return el('label', { className: 'srw-provider-filter-item' }, [cb, ` ${providerLabel(key)}`]);
      });
      parts.push(el('span', { className: 'srw-provider-filter' }, boxes));
    }
    return el('div', { className: 'srw-filter-bar' }, parts);
  }

  function buildPagerControls(page, totalPages, total, onPage, perPage = null) {
    return buildPager({ page, totalPages, total, perPage, onPage });
  }

  function buildFilteredPagedList({
    filters = [], fetchPage, renderItems, perPage = 40,
    emptyText = 'Nothing here.', filteredEmptyText = 'Nothing matches the current filter.',
    onTotal = null, showFilterBarFrom = 0, pairingOptions = [], castOptions = [], barTarget = null,
  }) {
    const state = defaultFilterState();
    state.pairingOptions = pairingOptions;
    state.castOptions = castOptions;
    const listArea = el('div', { className: 'srw-paged-list' });
    const barSlot = el('div');
    const node = el('div', { className: 'srw-filtered-paged' }, [barSlot, listArea]);
    let page = 1;
    let seq = 0;
    let barShown = false;

    async function load(targetPage = page) {
      const mySeq = ++seq;
      let result;
      try {
        result = await fetchPage(state, targetPage, perPage);
      } catch (e) {
        result = { error: e.message };
      }
      if (mySeq !== seq) return; // a newer filter change/page switch is on its way
      listArea.innerHTML = '';
      if (!result || result.error) {
        listArea.appendChild(el('p', { textContent: `Error: ${(result && result.error) || 'unknown'}` }));
        return;
      }
      const active = isFilterActive(state);
      const totalPages = Math.max(1, Math.ceil(result.total / perPage));
      if (targetPage > totalPages && result.total > 0) { load(totalPages); return; }
      page = targetPage;
      // The filter bar only appears once the list is big enough to need
      // it (or a filter is already active) -- a 3-entry list doesn't get
      // a full toolbar.
      if (!barShown && filters.length && (result.total > showFilterBarFrom || active)) {
        (barTarget || barSlot).appendChild(buildPagedFilterBar(state, filters, () => load(1)));
        barShown = true;
      }
      if (onTotal) onTotal(result.total, active);
      if (!result.items.length) {
        listArea.appendChild(el('p', { className: 'srw-hint', textContent: active ? filteredEmptyText : emptyText }));
        return;
      }
      if (totalPages > 1) listArea.appendChild(buildPagerControls(page, totalPages, result.total, load, perPage));
      listArea.appendChild(renderItems(result.items));
      if (totalPages > 1) listArea.appendChild(buildPagerControls(page, totalPages, result.total, load, perPage));
    }

    return {
      node,
      state,
      reload: () => load(page),
      reset: () => load(1),
      // Jump helper: narrow the list to one entry by its text (e.g. a
      // scene title) -- rows on other pages aren't in the DOM any more.
      setSearch(text) {
        state.search = text || '';
        const input = (barTarget || barSlot).querySelector('.srw-search-input');
        if (input) input.value = state.search;
        return load(1);
      },
    };
  }

  // Client-side fetchPage for an already-loaded array.
  function localPageFetcher(getItems, fields) {
    return async (f, page, perPage) => {
      const items = filterLocally(await getItems(), f, fields);
      return { total: items.length, items: items.slice((page - 1) * perPage, page * perPage) };
    };
  }

  // Back-catalogue additions (2026-09-26): scenes the scan routed to the
  // backlog because they were added to StashDB/ThePornDB long after their
  // release date (api/watcher.py's _is_late_addition()). Marked "New" for
  // LATE_NEW_DAYS after discovery -- badge + a coloured row accent
  // (srw-late-new on the row).
  const LATE_NEW_DAYS = 30;

  function isLateAdditionNew(r) {
    if (!r || !r.late_addition || r.status !== 'backlog' || !r.discovered_at) return false;
    const discovered = new Date(`${r.discovered_at.replace(' ', 'T')}Z`);
    return (Date.now() - discovered.getTime()) / 86400000 <= LATE_NEW_DAYS;
  }

  function buildLateAdditionBadge(r) {
    if (!r || !r.late_addition) return null;
    const isNew = isLateAdditionNew(r);
    return el('span', {
      className: `srw-late-badge${isNew ? ' srw-late-badge-new' : ''}`,
      title: `Back-catalogue addition: released ${r.scene_date || '?'}, added to `
        + `${providerLabel(r.provider)} on ${(r.scene_created || '').slice(0, 10)}`
        + ` -- found by the scan ${String(r.discovered_at || '').slice(0, 10)}`,
      textContent: isNew ? 'New' : 'Added late',
    });
  }

  // ==================== Live push (2026-09-26, v0.78.0) ====================
  //
  // One WebSocket per browser tab, straight to the sidecar's /events/ws
  // (see api/events.py) instead of polling through runPluginOperation --
  // the user's "hooks instead of polling" principle. WebSocket, not
  // EventSource: Stash's Content-Security-Policy only allows `connect-src
  // 'self' ws: wss:` (checked live), so EventSource/fetch to the sidecar
  // are blocked, WebSockets aren't. The browser must reach sidecarUrl
  // itself; if it can't (other network; HTTPS Stash needs wss:// = a TLS
  // proxy) pages fall back to their old polling and show a hint.
  // WebSocket address of a sidecar path. A page opened over https cannot open ws:// (mixed content, Firefox and Chrome refuse it, and a
  // private address is not reachable from outside anyway): there the socket goes to the page's own origin (wss://<host><path>), which
  // the reverse proxy in front of Stash forwards to the sidecar -- a location per path (/events/ws, /transfer/upload).
  function sidecarWsUrl(sidecarUrl, path) {
    if (window.location.protocol === 'https:' && /^http:/i.test(sidecarUrl)) return `wss://${window.location.host}${path}`;
    return `${sidecarUrl.replace(/^http/, 'ws')}${path}`;
  }

  const LIVE_RECONNECT_MS = 5000;
  // Following a link to another page closes the stream first; that is not "live updates unavailable".
  let leavingPage = false;
  window.addEventListener('pagehide', () => { leavingPage = true; });
  window.addEventListener('beforeunload', () => { leavingPage = true; });
  window.addEventListener('pageshow', () => { leavingPage = false; });
  const liveListeners = new Set();
  let liveSocket = null;
  let liveState = null; // null = connecting, true = open, false = unavailable
  let liveConnectTimer = null;
  let liveReconnectTimer = null;

  function setLiveState(open) {
    if (open === liveState) return;
    liveState = open;
    liveListeners.forEach((fn) => fn({ type: 'live', live: open }));
  }

  async function ensureLiveSource() {
    if (liveSocket) return;
    if (typeof WebSocket === 'undefined') { setLiveState(false); return; }
    const { sidecarUrl } = await getSettings();
    if (liveSocket || !liveListeners.size) return;
    let socket;
    try {
      socket = new WebSocket(sidecarWsUrl(sidecarUrl, '/events/ws'));
    } catch (e) {
      setLiveState(false);
      return;
    }
    liveSocket = socket;
    socket.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch { return; }
      if (msg.type === 'hello') setLiveState(true);
      liveListeners.forEach((fn) => fn(msg));
    };
    socket.onclose = () => {
      if (liveSocket !== socket || leavingPage) return; // closed on purpose (no listeners left), or because the page is being left
      liveSocket = null;
      setLiveState(false);
      clearTimeout(liveReconnectTimer);
      liveReconnectTimer = setTimeout(() => { if (liveListeners.size) ensureLiveSource(); }, LIVE_RECONNECT_MS);
    };
    clearTimeout(liveConnectTimer);
    liveConnectTimer = setTimeout(() => { if (liveState === null) setLiveState(false); }, 4000);
  }

  function subscribeLive(fn) {
    liveListeners.add(fn);
    if (liveState !== null) setTimeout(() => fn({ type: 'live', live: liveState }), 0);
    ensureLiveSource();
    return () => {
      liveListeners.delete(fn);
      if (!liveListeners.size) {
        const socket = liveSocket;
        liveSocket = null;
        liveState = null;
        clearTimeout(liveConnectTimer);
        clearTimeout(liveReconnectTimer);
        if (socket) socket.close();
      }
    };
  }

  // Page-level wiring: `onProgress(map)` for 'progress' events,
  // `onChange(msg)` (debounced 500 ms) for 'release_changed', `onResync()`
  // after a reconnect. While the stream is unavailable `poll()` runs every
  // `pollMs` (the old behavior) and `hint` is shown; while it's live,
  // `poll()` still runs every `safetyMs` as a safety net for a missed
  // event. Detaches itself once `anchor` (a node of the page's own
  // content -- NOT the shared plugin root, every page renders into that
  // same element) left the document. Returns detach().
  function attachLive(anchor, { onProgress, onChange, onResync, onEvent, poll, pollMs = 10000, safetyMs = 60000, hint }) {
    let timer = null;
    let changeTimer = null;
    let hintTimer = null;
    let helloSeen = false;
    const gone = () => !anchor || !document.body.contains(anchor);
    const setTimer = (ms) => {
      if (timer) clearInterval(timer);
      timer = setInterval(() => { if (gone()) detach(); else if (poll) poll(); }, ms);
    };
    function detach() {
      unsubscribe();
      if (timer) clearInterval(timer);
      timer = null;
      clearTimeout(changeTimer);
      clearTimeout(hintTimer);
    }
    const unsubscribe = subscribeLive((msg) => {
      if (gone()) { detach(); return; }
      if (msg.type === 'live') {
        if (hint) { // shown only when the stream stays down for a few seconds (a reconnect or a page change is no reason to flash it)
          clearTimeout(hintTimer);
          if (msg.live) hint.hidden = true;
          else hintTimer = setTimeout(() => { hint.hidden = false; }, 3000);
        }
        setTimer(msg.live ? safetyMs : pollMs);
        if (!msg.live && poll) poll();
      } else if (msg.type === 'hello') {
        if (helloSeen && onResync) onResync();
        helloSeen = true;
      } else if (msg.type === 'progress') {
        if (onProgress) onProgress(msg.data || {});
      } else if (msg.type === 'release_changed') {
        clearTimeout(changeTimer);
        changeTimer = setTimeout(() => { if (!gone() && onChange) onChange(msg); }, 500);
      } else if (onEvent && msg.type !== 'ping') {
        onEvent(msg); // e.g. 'history' (History page)
      }
    });
    // Until the stream answers (or gives up after 4 s), poll as before.
    setTimer(pollMs);
    return detach;
  }

  function buildLiveHint() {
    return el('p', {
      className: 'srw-hint srw-live-hint', hidden: true,
      textContent: 'Live updates unavailable (this browser can\'t reach the sidecar directly) — refreshing every few seconds instead.',
    });
  }

  // Settings > Content (v0.139.0): what the plugin looks after -- 'both', 'scenes' or 'movies' (movies/groups). Read once per page load.
  let contentScopePromise = null;
  function getContentScope() {
    if (!contentScopePromise) {
      contentScopePromise = runOp('settings_get_all')
        .then((st) => (st && ['scenes', 'movies'].includes(st.content_scope) ? st.content_scope : 'both'))
        .catch(() => 'both');
    }
    return contentScopePromise;
  }
  function resetContentScope() { contentScopePromise = null; }
  async function scopeAllows(kind) { const scope = await getContentScope(); return scope === 'both' || scope === kind; }

  window.SRW = {
    PLUGIN_ID, PLUGIN_NAME,
    getContentScope, resetContentScope, scopeAllows,
    getSettings, runOp, sidecarWsUrl, stashUrl, copyText,
    getRoute, onNavigate, initNavigationWatcher,
    el, waitForElement, entityIcon, buildPageHeader,
    watchKind, keepResult, recallResult, forgetResult, keptNote, searchStatusLabel, setBusy, openModal, sceneSubtitle, iconButton, busyIcon, ageWords, ageWarning, confirmInline, buildManagedList, buildSearchActions, whenNoOpenPanel, buildManualSearch, getSearchCapabilities,
    rememberOpen, icon, mismatchWarning, buildPagedList, buildPager, releaseTitleNode, notify, dismissedNotice, releaseDismissedNotice, dismissRelease, buildWishlistDismiss, buildFilmDismissButtons, notifyHandoff, buildAddOnlyButton,
    sceneUrl, notifyReadd, loadMetadataProviderUrls, loadProviders, resetProviders, providerInfo, providerLabel, providerKeys, providerOn, providersAllOn, providersSelected, isLateAdditionNew, buildLateAdditionBadge,
    buildFilteredPagedList, buildSelect, buildSafetyPanel, ignoreRenamers, pairingOptionsFor, castOptionsFor, buildPairingBadge, localPageFetcher, releaseQueryArgs, filterLocally, isFilterActive,
    describeProgress, applyProgress, attachLive, buildLiveHint,
    buildEntityIcons, buildLocalEntityLink, buildReleaseThumb, buildStudioContext, fillCast, groupByScene,
    SENT_STATUS_LABEL, SENT_STATUS_LABEL_LONG,
    buildStashLink, createScopeStateRegistry, buildSideNav, setActivityCount, acknowledgeUrl, buildPaneTabs, sectionTabLabel, arrangeReleaseRow, rowMeta, addOpenSceneLink, resolveSceneLinks,
  };
  startActivityWatch();
  loadProviders();
})();
