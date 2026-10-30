# Changelog

Short summary of what each version added; versions follow SemVer.

Version rules (from 0.199.0 on; before, almost every change raised the MINOR number): PATCH = a fix or a refinement of something that exists;
MINOR = a new capability a user can use, or a change that needs the user to act; MAJOR = something that worked breaks. 1.0.0 comes when the
install guide has been proven on other machines. Documentation only gets no version.

## 0.246.12 (with 0.246.11)
- **Userscript: feedback pop-up.** A short pop-up in the bottom right corner tells you whether the hand-off to the sidecar worked ("added to the wishlist", "searching now", "already on the wishlist", "Stash already has this scene", "added to / removed from the watchlist") or failed (in red, with the reason, and it stays longer). Update the script in your userscript manager afterwards.
- **Fix: the number on the Activity tab could be higher than its list.** A "download finished" note of a release that had been sent back to searching was never cleared and counted as a running import until the container restarted. Such notes are now dropped.

## 0.246.10 (with 0.246.3 - 0.246.9)
- **Shared fingerprints, refined.** The provider's lookup by picture hash (PHASH) is a **similarity** search with a few bits of tolerance, so it also returns scenes that are other videos with the same frame layout (VR scenes, black intros ...). Only scenes that have an **exactly equal** hash registered (OSHASH, or PHASH at distance 0) now count as owners of a file's fingerprint; the similar-only ones are set aside, listed in a fold-away note per row, and no longer make a file "ambiguous". A real wrong submission at the provider (someone's exact hashes on an unrelated scene) stays listed. A new scan settles the rows that are fine now.
- **Download verification** leaves a hint in History (and a row on the list) when a verified download's fingerprint is registered at several scenes and the length does not single the scene out; the import is not stopped.
- **List improvements:** the button **Confirm the harmless ones** (tag fits the length, all other fitting scenes belong to the same studio, the file already sits in that studio's folder, the title is the tag's), **Play in Stash at the top** of every row plus **Show frames from the file**, the provider scene names are links to the stash-box page (also on the Fingerprint check page), the tagged scene is highlighted, scene and group links open in a new tab, and the "Use this scene" confirmation can be clicked again (it lay under the next row). Needs the container rebuild for the verification changes; the rest is plugin only.

## 0.246.2 (with 0.246.0 - 0.246.1)
- **Fix: a fingerprint that is registered at several provider scenes no longer counts as proof.** Fingerprints are crowd-sourced, and when the same footage is released twice (another site re-sells it) the same hash sits on two or more scenes of the provider. A file used to count as "verified" as soon as the scene it is tagged with was among them, so a file could be planned for the wrong studio folder. Now the **length** decides (6 % tolerance): the tag is trusted only when the provider's length for it fits the file and no other candidate fits too. Otherwise the file is "ambiguous": Organize library and the Organized hook put it under "needs check" with the reason, Import folder does not trust it, the Fingerprint check lists it under "Needs manual review" with the reason and the candidates, and "Check" / "Re-check fingerprints" say so. A verified download whose fingerprint is not unique leaves a hint in History.
- **New: Organize library > Shared fingerprints.** The list of those scenes (kept in the database until each is settled): the file's length and every provider scene it is registered at (studio, title, date, length, chips "length fits the file", "same title as your scene", "tagged now"). Per scene: "Use this scene" (the scene gets that provider scene's id and metadata after its state is saved; rename / move afterwards under "Scenes by studio"), "Tag is right" (not listed again) or "Dismiss". "Scan the library" looks up every tagged scene once and settles rows that are fine now. Needs the container rebuild.

## 0.245.4 (with 0.245.0 - 0.245.3)
- **New: Maintenance > Convert.** Library files that no browser plays directly are turned into H.264 + AAC in an MP4 of the same picture size, by an **encode worker** (never the NAS). *Re-encode* is for an old video codec (MPEG-4 in AVI, WMV, FLV1 / VP6, MPEG-1 ...): x264 with a quality target and a ceiling of the original's own bit rate, so a file does not grow; AAC / MP3 sound is copied, other sound becomes AAC at the original's bit rate (48 - 128 kbit/s). *Repack* is for good H.264 in an old container (FLV ...): only the container changes, lossless, in seconds. HEVC, VR files and files whose only problem is AC3 sound are not part of it. "Find candidates" reads Stash (files the upgrade search can work on, those with a provider id, are left out); you select several at once, press Convert, and the worker does them one after the other, with progress per row and Cancel. The Stash scene stays: the converted file joins it (scene merge), becomes its primary file, and only then does the old file go to the recycle bin; a run cut off by a restart continues, a failure leaves the original untouched. The new file keeps the name with the ending .mp4. Real example: a 114-minute AVI went from 1.5 GB to 0.9 GB in 4.5 minutes. The queue lives in the database.
- **New settings** (Settings > Quality, "Where to encode"): *Library on the worker* and *Downloads on the worker*, the folders as the worker machine mounts them. They are sent with every job, so the worker needs no `--map` option any more. The worker has to be restarted after the update.

## 0.244.0
- **The library tools use the stash-boxes you added yourself.** Import folder, Unidentified scenes, Compare scenes (identify, search, use), Organize library (the fingerprint check before a rename or move) and the "Organized" hook used to know StashDB and ThePornDB only. They now ask StashDB (when set up), then your own stash-boxes (one batch request per 40 files, only for files the earlier ones do not know), then ThePornDB (exact hashes). A match keeps its provider: "Assign" / "Use this" puts the id on that stash-box's endpoint, the confirmation names it, and the link opens its scene page. A scene that carries only an id of your stash-box is recognised and checked against it. Needs the container rebuild.

## 0.243.21 (with 0.243.19 - 0.243.20)
- **Performers without scenes: several at once.** Every card has a checkbox; "select this page" sits in the toolbar and "Select all N matching" in the bulk bar (the search text and the "hide the ones on the watchlist" switch count). The bar offers "Remove from watchlist (n)" (only the selected ones that are on it; they stay in Stash), "Delete (n)" (red, asks first, cannot be undone) and "Clear selection". The actions run one performer after the other with a counter; one that cannot be deleted stays selected and is counted.
- **The page stays where it is** after you remove or change ONE entry of a long list (it used to jump back to page 1): Performers without scenes, Unidentified scenes, duration findings, Compare groups and Movies & groups > Already in the library. Filter and sort changes still start at page 1.
- **Fix:** the helper that builds the page elements wrote `disabled: false` / `checked: false` as the text "false", which the browser counts as set. The button "Start fingerprint check" (0.243.0) was disabled because of it, and two checkboxes started checked ("hide the ones on the watchlist", and the groups of the Export dialog that are off by default). Fixed for every element.

## 0.243.18 (with 0.242.1 - 0.243.17)
- **Fingerprint tools use the provider you have set up.** Maintenance > Fingerprint check asks StashDB, ThePornDB or a stash-box you added, whichever is set up; with several there is a "Check against" drop-down (the last one used is the default). A run remembers its provider, so a finding's re-check, correction and "File is correct" use the same one. ThePornDB has no batch lookup and is asked one file at a time, with exact hashes only (OSHASH / MD5, never its similarity-based PHASH); the page says that a check of the whole library takes long and you can pick directories instead. Without a StashDB key, Unidentified scenes, Import folder and Organize library (including "Check first") work with ThePornDB alone. Stash-boxes you added are used by the Fingerprint check only for now.
- **Setup guide:** a new optional step "Browser userscript" explains what the userscript does and how to install it (userscript manager, the sidecar's script address with a copy button). "Copy" works over plain http as well.
- **Icons instead of long text buttons** (words stay as tooltips): Reload for the plain "Refresh" buttons, a list icon for "Scenes..." and "Details...", magnifier + list for "Check StashDB scenes", a chain link for "Link movie...", a clipboard with a tick for "Check", counter-clockwise arrows for "Restore" and (with a magnifier) "Last search", the magnifier for "Search now" on scenes as well, and on Activity > Import failed "Search again", "Retry the encode" and "Group anyway". Destructive buttons and the Settings forms keep their words; the delete buttons of a failed movie import are "Delete + search" and "Delete + dismiss".
- **Look:** drop-down fields show an arrow again, every button reacts to the mouse (hover and keyboard focus), the filter bars follow the Stash theme, the movie rows' search buttons are one joined group in the buttons' column like the scene rows', and the number badges in the Maintenance menu are centered in their pill. Destructive buttons are red again (the rule for them never took effect). Taken over from the fork of AnonTester: pictures from Stash are loaded by path from the address the page was opened with (Stash hands out its picture links with its internal host, which the browser blocks as a "Local Network Access" request), and the centered menu badges.
- **Fixes:** "Delete + search" / "Delete + dismiss" on a failed movie import ended with HTTP 500 after the files were in the recycle bin (a variable was never set; the movie stayed in Import failed, pressing the button again finishes it); the opened list of files under a movie row on Import failed no longer breaks the layout; the cover of a movie without a Stash Group says "No group in Stash yet" instead of doing nothing; the space in front of "Dismiss" is only in front of the first of the two Dismiss buttons.
- INSTALL.md: a troubleshooting row for stuttering playback (the browser cannot decode the file, e.g. H.264 in a Chromium without proprietary codecs, so Stash transcodes live on the server; use *Direct stream* to test).

## 0.242.0 (with 0.238.1 - 0.241.10)
- **One look for all of Maintenance.** Sections are tabs with their numbers, the details of a list entry open in a wide dialog, rows share one layout, long harmless buttons are icon buttons (the words are the tooltip), changing actions keep their words and a question, and fields, dropdown lists and the video bar follow the Stash theme. The side menu shows a number behind every page that has something waiting for a decision.
- **Results are kept.** What a check found (the Organize plans, the unidentified scenes, the movie and group lists, provider look-ups per scene, the last Search) is kept in the sidecar or in the browser until a change takes an entry out of it, so opening a page again does not repeat the work; the button "Reload" (circular arrow) asks again.
- **Organize library works like Whisparr's Stash hook:** rename and move return at once (the files are moved, Whisparr and the sidecar's tables follow, ONE Stash scan is started and not waited for, a check in the background reports a miss in History). "Recount" checks every studio against StashDB in the background and keeps the plans; "Details..." shows them; the target folder is the studio StashDB gives the scene itself (a scene of a child studio goes to that folder). Rows that wait for a fingerprint check can be checked, sent to Identify Scene or renamed anyway.
- **Duration mismatch moved into Upgrade wanted** (reason "wrong length": pill "file shorter / file longer", threshold, direction filter, "Run duration check"); candidates wait in "To check" until you release them. Scenes of movie groups and files in movie folders have their own page (Movies > Duration: movie scenes).
- **VR page = the place where the check's doubts are settled:** only unclear files and files the check judged flat although the scene is tagged VR; you say 2D, VR mono or Stereo (3D); a confirmed 2D / mono file goes to Upgrade wanted.
- **Fingerprint check:** rows that StashDB has no fingerprint for get "File is correct" and "Correct + submit to StashDB" (the latter only while Settings allows submitting).
- **Movies & groups:** "Compare groups" is a tab (it replaces "Groups without files"): every empty group, matched with a video, as a duplicate of a group that has files (delete the empty one or dismiss), or without a suggestion.
- **Search and userscript:** the cards show the watchlist state and offer add / remove (the eye and the crossed-out eye with the watchlist, the heart with Stash favorites; only the kind chosen in Settings); watching a studio whose name exists in Stash links it instead of failing; a newly watched entry shows on the Watchlist page at once. The userscript's marks no longer cover StashDB's own dropdown lists.
- Other: "Performers without scenes" as cards (remove from watchlist, delete), "Send to Identify Scene" from Upgrade wanted rows without an id, icon buttons for Now / Last / Manual, the review of a scene in a dialog, a player bar that follows the theme.

## 0.238.0
- Maintenance: the page "Library check" is now **Fingerprint check** (it compares your files' fingerprints with the ones StashDB has registered and finds scenes whose StashDB ID is wrong); the address is unchanged. Its "Needs manual review" rows have a **Re-check fingerprints** button: it asks StashDB again what the file is (fingerprints may have been registered since the check ran) -- a match closes the finding, one other scene turns it into a correction, several scenes make it "ambiguous".

## 0.237.0 - 0.237.8
- **Maintenance > Upgrade wanted reworked.** Rows with a checkbox, screenshot, reason badge, title, chips (resolution, size, bitrate, length, studio, date, own quality profile), state and path. Filters: reason, state (Open / To check / Downloading / On the wishlist / Dismissed / Done / All, each with its number), scenes without an id; a reversible sort (title, size, resolution, bitrate, last searched, scene date, reason). Bulk actions on the selected rows ("Select all N matching"): Dismiss and **To wishlist** (both with Undo), Restore, Search now (at most 10 at a time), Queue for search, and a **quality profile per candidate** (empty = the profile of the Settings; it counts for non-VR files, VR upgrades follow the VR rules). Per row: Search now, Manual (in a modal), To wishlist, Dismiss in Stash's own style. The Dry run button is hidden; Settings > Jobs > Upgrade search > "Dry-run button on Upgrade wanted" shows it again (its result opens in a modal).
- **To wishlist** makes the candidate a normal wishlist release (marked with the file it replaces); the regular wishlist search handles it and the old file goes to the recycle bin after a verified import. Releases > On Wishlist and Activity > Stuck have a filter "With upgrades / Hide upgrades / Only upgrades".
- **Upgrades never take a worse file:** a scene that is being upgraded (the upgrade search, "To wishlist", a re-add) is held by EVERY search -- the wishlist's own, RSS and targeted ones included -- to taking only a release above the file in the library; a name without a resolution is not accepted and the last-resort kinds of the normal wishlist (2160p scaled down, SD after the configured days) do not apply.
- **Check the whole library** (button on Upgrade wanted, started by you, opt-in): looks at every scene of the library once. It runs the duration check over the whole library (the providers' lengths are kept for 30 days, so a second run is quick) and adds files that are clearly shorter or longer (5 %) as candidates; then it looks for 2D / mono VR files, VR files below a bitrate (Settings > Jobs > Upgrade search: default 8 Mbit/s; before: a width / bitrate rule) and files below the cutoff resolution of the quality profile (scenes in Groups -- films -- are left out). It runs in the background with progress; the result of the last run is kept and shown on the next visit.
- Wrong-length candidates found by the library check wait in the state **"To check"** and are not searched automatically (a length that disagrees with the provider's is as often the provider's mistake as the file's); "Queue for search" releases them, "To wishlist" / Search now / Manual / Dismiss decide at once.
- The candidate refresh no longer fails on a scene that is gone from Stash.
- New sidecar parts: POST /maintenance/upgrades/bulk, /library-check/start and /status, column upgrade_candidate.quality_profile_id, settings upgrade_dry_run_button and vr_upgrade_min_mbps.

## 0.236.0 - 0.236.9
- **Scan:** a provider feed that fails (a request timing out on a deep page; read timeout 15 s) now falls back to the per-entry scan like a feed that cannot reach back to its watermark -- before, the whole provider was skipped. A clean fallback also moves the feed watermark forward (to 30 minutes before now), so the next feed read is short again; before, the fallback never advanced it and the feed fell further behind with every run.
- **Search modal:** "Last" and "Manual" open in a modal (Stash's own dialog markup, so a theme restyles it) instead of pushing a panel under the row. Sort is a button group (Best match / Indexer order, Age, Size, Quality, Seeders, Name; a second click reverses), the search field and the VR choice sit in the same row, each result is a row with chips (indexer, quality, size, age) and the Grab button on the right, and the scene (title, performers, studio, date) is shown under the heading. "Now" keeps its status text, now under the buttons so they do not move.
- **Release age:** the possible match on On Wishlist shows how old the release is ("34 days old", "412 days old (about 14 months)") and warns "older than the scene" when it was posted more than 30 days before the scene's own date (also in Manual and Last).
- **Confirmations never shift anything:** every question ("...? Yes / Cancel") is an overlay over the button that was clicked (as in a calendar pill); the result text of a row's buttons has its own line.
- **Undo** for "Not this one" (On Wishlist) and "Reject" (Needs review) of a possible match: the candidate comes back for review (kept in the sidecar's memory for the Undo; gone after a restart).
- **Icon buttons** (a shared helper, the words are the tooltip) on Activity > Import failed (play, fingerprint, check, upload) and for "Search now" / "Open group" on movie rows; Dismiss buttons use Stash's own danger styles. A spinner shows in front of every running search status. Activity > Import failed: **Re-check fingerprints** asks the provider again; an exact match imports the file, otherwise it says how many fingerprints the provider has and how close the nearest perceptual hash is (only an exact match imports automatically).

## 0.235.8
- Dismiss buttons use Stash's own button classes and follow its theme: "Dismiss" is the outlined red, "Dismiss & Delete" the filled red of Stash's Delete button.
- Movie rows (Activity > Stuck, Movies page): "Search now" and "Open group" are icon buttons (words as tooltip); "Search now" shows a spinner while it runs.

## 0.235.2 - 0.235.7
- Activity > Import failed: **Re-check fingerprints** asks the provider for the scene's fingerprints again (someone may have submitted some since the download was judged). An exact match imports the file like a verified one; otherwise nothing changes and the row says how many fingerprints the provider has and how close the nearest perceptual hash is. Only an exact match imports automatically (the 8-bit tolerance is for files this sidecar re-encoded itself); a file within 8 bits is very likely the same video in another encode, and "File is correct" is the way to keep it.
- The buttons of that row are icon buttons (play, fingerprint, check, upload; the words are the tooltip), "Fix wrong download" keeps its text. The "Correct + submit to StashDB" button is hidden while submitting fingerprints is switched off in Settings.
- Release rows: the result / progress text of a row's buttons has its own line under the row's text (it used to widen the buttons' column and shift the list), and the confirmation question of a button is an overlay over the buttons' place, as in a calendar pill, so nothing moves. Needs review keeps its own layout.

## 0.234.5
- A running search shows a small spinner in front of its status text everywhere (Now, Last, Manual, the Search page, Maintenance searches, film searches, the indexer test, the torrent actions on Activity).

## 0.233.0 - 0.234.4
- Browser userscript for stash-box sites (Tampermonkey or Violentmonkey), served by the sidecar at `/userscript/srm.user.js` (see INSTALL.md). On StashDB scene pages and scene cards: **Send to wishlist** (searched by the schedule), **Send and search now**, **Open in Stash** (for every scene in your whole Stash library, not only the wishlist), **Copy Stash-ID**, and a status colour (the colours of the Release Calendar: green imported / in the library, purple on the wishlist, blue found by a scan, red import failed, grey dismissed). A scene page shows two chips, "Stash" and "SRM"; on a card only a status dot shows until the pointer is on it; dots pulse while the status loads.
- On performer and studio pages: the same chips and an eye button to **Watch** (created in Stash first when missing; Favorite flag or watchlist tag, as set in the plugin) or, crossed out, to take it off the watchlist again (only the mark is removed, nothing is deleted in Stash), plus Open in Stash.
- New endpoints: GET /userscript/state, POST /userscript/wishlist, GET /userscript/entity-state, POST /userscript/watch.

## 0.232.1 - 0.232.3
- Activity > Downloading & importing: a download that waits for its re-encode says "Waiting for the re-encode" and who is expected to do it. With the encode worker offline the text is amber and tells you to start it (or use "Encode on the NAS now"); every variant shows how long the job has waited.
- History: a scan that had failed entries lists them in the entry's details (provider, kind, name, error; up to 25).
- Encode worker: a sidecar restart no longer throws away a running encode. The worker keeps encoding when a progress report does not get through, the sidecar tells a worker that keeps reporting "still yours" during the first ten minutes after a start and hands the queued job back to the worker that is encoding it, and the final report is retried every 30 s for 15 minutes. A worker that is already running keeps the old behaviour until it is restarted (do that between two encodes).

## 0.232.0
- Maintenance > Duration mismatch has the layout of Upgrade wanted (colored badge, screenshot, title linking to the scene, lengths, buttons in one group).
- Maintenance > Upgrade wanted can be filtered by why a file is on the list: 2D file -> real VR, mono VR -> stereo VR, VR below 4K, quality below cutoff (all, or by the current file's resolution), re-added, wrong length, done; every choice shows its count. Scenes without a provider id are hidden by default (a checkbox shows them).

## 0.230.1 - 0.231.7
- **Torrents are watched** (Settings > Search & Quality > Torrents; see INSTALL.md). Every 30 minutes the running torrents of scenes and movies are checked. A torrent without progress for an hour (almost nothing downloaded) is replaced by another torrent when the search finds one: the dead one is removed from qBittorrent with its partial data and never used again (blocked by its hash, not its name). Without another torrent it is shown as stuck and searched for again every 6 hours. A torrent with real progress that stalls, one in an error state or one gone from the client is only flagged. Activity > Stuck lists them with seeds, peers and availability and the buttons Find another, Alternatives, Re-add, Keep waiting and Remove. Only observed time counts: while the sidecar or the download client was not running nothing is concluded from the silence. A search that could not be made (indexer not answering) is no longer taken for "no other torrent exists"; with the indexers down the best torrent of the last search is added from its magnet link.
- Torrent choice: seeders count. The best resolution wins when it has plenty of seeders (default 10), otherwise a lower resolution with at least three times as many; inside a resolution the most seeders win. A release name without a resolution is judged from the scene's length and the size; the search log and the manual search show seeders, peers, the estimated resolution and a "size?" note. The duplicate guard and the torrent blocklist use the info hash.
- The search log ("Last") shows seeders and peers and has a Grab button on every result; it keeps up to 500 results per search and is only loaded when "Last" is opened. Release names that are really a post text are cleaned for display.
- A download link that answers with a redirect to a magnet link is added to qBittorrent as that magnet link (before: "InvalidSchema").
- The status of a torrent on Activity, Releases > On Wishlist and Stuck shows the download client's numbers of this moment (seeds, peers, availability, stall time). The Activity number is there from the first moment; the "live updates unavailable" notice no longer flashes when a tab is clicked.
- Phone screens: the buttons of Releases > On Wishlist and the Activity tabs wrap under the text. The browser tab of the plugin's pages is titled "SRM Plugin | <Stash title>". Settings > Connections: Firefox no longer offers to save API keys as logins. History: "Added to the wishlist" is written before the search's own entries.

## 0.230.0
- Indexer search: a scene whose StashDB studio is a performer-named creator but whose release carries the SITE's name ("Site.Performer.Title.Words...") is found now. (1) The scene's "Studio" link names the site (`https://www.example-site.com/watch/...` -> `examplesite`): the queries "<site> <first performer>" and "<site> YY.MM.DD" are added after the existing ones, except when the site is the studio's own name or part of it. The links are fetched from StashDB once per scene at its first search and stored; ThePornDB scenes are not asked. (2) A title that ends in " - <cast>" is also searched without that tail, right after the full title: no release name carries it and the indexers want every word. The queries stay staged: the search stops at the first plausible lead.

## 0.228.5
- Import folder: a scene that was sent to Review: Identify Scene shows "in Review: Identify Scene" instead of the "Send to Identify Scene" button, also after a reload or a new analysis.

## 0.228.4
- Import folder: "Refresh" also checks the analysis on screen and makes it again when the folder changed since (a file that was moved meanwhile no longer stays in the list).

## 0.228.3
- Maintenance > Unidentified scenes: "Park in _UNMATCHED" on every row moves the file of a scene no provider knows to `scenes/_UNMATCHED/<its folders>`, like Review: Identify Scene's "Move to _UNMATCHED". The Stash scene and its data stay; a backup is made.

## 0.228.0 - 0.228.2
- Import folder > Needs a look: "Import anyway (I checked it)" for a scene that has a provider id but whose file the provider cannot confirm by fingerprint (no fingerprints stored there, they differ, no local phash yet, the lookup failed, or a near match of a file older than the release date). After you looked at the file it is placed like a verified one; History records that you confirmed it. A provider scene another scene already holds, several files and a taken name are not overruled.
- Before the provider's data is written over a scene (every import, "Import anyway" included) the scene's metadata -- title, date, studio, performers, tags, ids, links, groups -- is saved as a JSON file in the backups folder; History names it. The confirm of "Import anyway" shows the scene's title next to what the provider says and states that the provider's data replaces the scene's.

## 0.227.2
- Import folder > "Complete via ThePornDB...": "Preview" opens the movie's ThePornDB page (it opened the shop page ThePornDB lists as the source).

## 0.227.1
- Import folder: the analysis kept from the last visit is only shown while nothing has changed. Every analysis carries a signature of what it depends on (files, provider ids, Groups and their links, Organized); when the current one differs (a Group was made, a link added, a scene marked Organized ...) the analysis is made again by itself.

## 0.227.0
- NEW Import folder > "Complete via ThePornDB...": for a file whose Group exists (e.g. scraped from a shop page) but has no ThePornDB movie link yet. It searches the movie at ThePornDB (one request; each hit shows year, studio and length next to the file's length, "length matches" within 10 %); "Link this movie" adds the ThePornDB page to the Group's links -- the import takes a Group with that link for a confirmed film -- and fills the Group's EMPTY fields (date, length, director, synopsis, a local studio). Nothing that is set is changed, the name and the covers stay. `POST /maintenance/movies/tpdb-search`, `POST /maintenance/movies/link-tpdb`.

## 0.226.1 - 0.226.3
- Import folder: the analysis result is shown as tabs (Ready / Movies / Identified / Needs a look / Unknown, each with its count) like the other pages. The source list loads itself ("Refresh" instead of "Load sources") and the last analysis is kept in the browser. Files no fingerprint knows get the hand tools of the other pages: "Play in Stash", "Make a movie..." (create the film's Group from a title or scrape it from a page, the same panel as Maintenance > Movies & groups) and "Send to Identify Scene".

## 0.226.0
- Import folder: the source folders are yours to choose. "Add folder" opens Stash's own folder browser; "Check this folder" is a dry run (video count, problems: the library itself, a folder that holds it, an unreadable or empty folder); "Add as import source" puts the folder on the list and, when no Stash library covers it, adds it to Stash's libraries (the old list is saved first) and starts a scan with phashes -- so folders Stash never saw can be imported too. "Remove from list" only takes a folder off the list; "Finish source" removes the finished folder from it as well. The fixed default folders are gone: a new installation starts with none. `POST /maintenance/import/add` (confirm), `GET /import/add-check`, `POST /import/remove`.
- Import folder, duplicates: a file whose provider scene another local scene already holds shows both sizes in the review text ("1920x1080 8.2 Mbit/s here, 1280x720 3.1 Mbit/s held: this one looks better"). A hint only; nothing is moved or deleted by it.

## 0.225.5
- Releases page (list view and calendar) and the Movies lists: Type (All types / Studios / Performer) and VR (VR + Non-VR / VR / Non-VR) are drop-downs now, like the other lists' filter bars. Presets keep working. (Plugin only.)

## 0.225.1 - 0.225.4
- Filter bar of the paged lists (Activity, History, Maintenance ...) stays on ONE row: the button groups became drop-downs ("All releases / Movies / Scenes" -- new, on lists that hold scenes and movies --, "All types / Studios / Performer", "VR + Non-VR / VR / Non-VR", "All additions / Added late / Not added late"). The drop-downs are readable on the dark bar and wide enough for their labels. (Plugin only.)

## 0.225.0
- Activity's Stuck / Downloading / Imported lists get an All | Movies | Scenes switch. (The "Filter (n)" menu that came with it was replaced in 0.225.1.)

## 0.224.0
- Activity > Stuck has two buttons because scenes and movies share the list: "Search all stuck scenes (N)" (one targeted indexer query per stuck scene release) and the new "Search all stuck movies (M)": the movie search (1-4 queries each) for every stuck movie, once, in the background. The confirm names the most queries it can send, a plausible release becomes a candidate in Needs review, nothing is downloaded, the result line lands in History. `GET /films/stuck` (preview), `POST /films/search-stuck`. A scene-only or movie-only Stuck list shows only its own button.

## 0.223.0
- NEW SABnzbd Archive purge: SABnzbd 4.2+ keeps a deleted history entry in its Archive and its duplicate detection reads it ("Duplicate NZB" for a release downloaded long ago). Once a day the sidecar removes the Archive entries older than `sab_archive_purge_days` (default 30, 0 = never; Settings > Schedule > "SABnzbd archive") from every enabled SABnzbd client -- only its category when the client has one, never a newer entry. After a run the archive is read again; if SABnzbd ignored the call a warning says so. `POST /download-clients/{id}/purge-archive` (dry run by default, `older_than_days`) does it by hand.
- Movie search: an apostrophe in the title no longer hides the release. "Joe's Movie 13" is written Joe.s.Movie.13 or Joes.Movie.13 in release names; a lone s / t / d / m / ll / re / ve now belongs to the word before it, and the plain spelling is asked as the last query for titles with an apostrophe.

## 0.222.2
- Movie search: a release of ONE SCENE is no longer offered as the movie. A scene release was only known by a four-digit date (2011.06.28); now a yy.mm.dd date (11.06.28) and a scene mark in the name ("Sc4", "Scene 2") count too.

## 0.222.1
- Movies > On Wishlist (Stuck): the manual search has "Grab as one scene" next to Grab. When the search finds only ONE scene of the movie, the file joins the movie's Group and the movie stays wanted for the other parts (like "Accept as one scene" in Needs review).
- The manual search of a movie marks the releases the movie rejected before ("tried before"), like the scene search; only the release's own name, not its other resolutions.

## 0.222.0
- NEW Upgrade search for movies. A movie on Movies > Upgrade (its file is below the quality profile) is searched for a BETTER release with the same movie search (same pace and per-run count as the wishlist; "Search now" on the row does it at once, and the manual search + Grab work on the row too). The release name must say a resolution that is at least the profile's smallest one and higher than the owned file's; a name without a resolution is not taken. A candidate waits in Activity > Needs review ("Upgrade · Movie"; Accept = download, Reject = never this release for this movie) -- nothing is downloaded by itself. Only a Group with exactly ONE scene and ONE file is searched.
- The import of an upgrade checks the length like any movie import, then merges the new file's scene INTO the owned scene (Stash sceneMerge, like a scene upgrade: metadata, rating, play history and the place in the Group stay), makes the new file primary and puts the old file in the recycle bin. If the owned Group is gone or the download has several files, both files stay and the movie is Import failed. Every failure (download failed or lost, unreadable file, "Wrong movie") sends the movie back to the Upgrade list, never to the wishlist, with the release blocklisted.

## 0.221.12
- Movies: a wishlist movie whose Stash Group has a file now that covers the movie (length within +-15 % of the ThePornDB length) leaves the wishlist and counts as imported with that Group. This runs before every automatic movie search; `POST /films/adopt-filled` (dry run by default) does it by hand. A file below the quality profile's smallest resolution puts the movie on the new Movies > Upgrade tab instead; "Keep this file" takes it off that list. A movie without a known length, or with parts added by hand, is left alone.

## 0.221.11
- NEW KDE Plasma 6 widget "SRM Encode Worker" (`worker/plasmoid`): shows what the encode worker does (state, file, progress bar, speed, time left, method, last result) and has Start / Stop / Restart buttons. The worker writes its state to `$XDG_RUNTIME_DIR/srm-encode-worker/status.json` (`--status-file`) and ends cleanly on SIGTERM / Ctrl+C (ffmpeg stopped, half file removed). `worker/install.sh` sets up the systemd user service (not enabled at login) and the widget.

## 0.221.10
- Manual search: the "tried before" mark is true again. It marks only the release's OWN name (before, a failed 2160p also marked the SD and the 1080p of the same release) and says why: "declined before" (declined in Needs review) or "tried before" (download failed or blocklisted). The badge sits next to the Grab button.

## 0.221.9
- The cleanup of SABnzbd's history removes entries for real (`archive=0`). SABnzbd 4.2+ only moves a deleted entry to its Archive, and its duplicate detection still reads the archive: a release this program had imported or rejected failed with "Duplicate NZB" when it was picked again. Entries archived before stay there until they are deleted from the Archive in SABnzbd (History > Show Archive).

## 0.221.8
- A download the client no longer knows (not in its queue, not in its history) and that was grabbed more than 30 minutes ago no longer waits for ever on "Download complete -- waiting for import": the job is dropped and the scene (or movie) goes to Stuck with "download lost". The release is not blocklisted.

## 0.221.7
- The cleanup of SABnzbd's history no longer removes the entry of a download whose import is still open.

## 0.221.6
- Activity > Import failed: "Retry the encode" for a release whose re-encode failed (the download is still in place). "Search again" would have left the downloaded file behind.

## 0.221.5
- Encode worker: the sidecar's check of a worker's result (decoding the whole file) runs in the background; the worker's "done" request used to wait for it and timed out on a NAS. The job is held ("checking the result") and cannot be handed to another worker meanwhile.

## 0.221.4
- A re-encode is checked by decoding the whole new file before it counts as done. A hardware encoder can report success and still write a broken stream (found with a VAAPI encode: CABAC errors). A damaged result is thrown away, the encode is repeated on the CPU, and a hardware encoder that wrote a damaged file is switched off until the next restart.

## 0.221.3
- A download the client reports as failed (e.g. SABnzbd "Duplicate NZB") no longer leaves the scene on Stuck with the status "grabbed": it says "download failed: <reason>".
- Manual search: a release this scene has been through already (rejected, blocklisted, failed download) carries a "tried before" mark, and Grab asks again before fetching it.

## 0.221.2
- Movies: a wishlist movie whose Stash Group was deleted is taken care of automatically before each movie search. A movie that came from an empty group is dismissed (Restore brings it back), any other movie just forgets the dead Group, so its cover no longer leads to "No group found". `POST /films/forget-dead-groups` does the same by hand (dry run by default).

## 0.221.1
- Movies: a movie whose remembered Stash Group was deleted in the meantime no longer fails at the import; a new Group is made from the movie's data.

## 0.221.0
- Movies, Activity > Needs review: "Accept as one scene" next to Accept. The found file is only one scene (part) of the movie: it skips the length check, joins the movie's Group (the next parts join the same one) and the movie stays on the wishlist, with the accepted release rejected for it so the search proposes the other parts. "All parts are in" on the Movies page takes the movie off the wishlist.

## 0.220.0
- Encode worker: Settings > Quality > "Where to encode" can hand the re-encode of a 2160p download to another machine (`worker/srm_encode_worker.py`, plain Python 3 plus ffmpeg; it only pulls jobs, a job waits while the machine is off). "Encode on the NAS now" on the waiting row and "Encode here if no worker takes it within (hours)" keep the queue from stalling. INSTALL.md has a section for it.

## 0.219.2
- A re-encoded scene is verified by its phash with a tolerance of 8 bits (its file hashes necessarily differ). A far-off phash gives the normal "different video" failure; a provider without fingerprints still imports it.

## 0.219.1
- After a re-encoded scene is imported, the 2160p original is removed from the download folder.

## 0.219.0
- The re-encode is its own stage: the import run only queues it and goes on with the other downloads. One background thread encodes one file at a time and then starts an import run that picks up the result.

## 0.218.7
- Re-encode settings: format H.265 (default) or H.264 (larger files, the only hardware encoder some chips have) and CPU speed medium / fast (default) / very fast. HDR sources stay 10-bit H.265 on the CPU. The hardware self-test runs per format.

## 0.218.6
- The hardware self-test also tries `-low_power 1` (the only HEVC encoder on some Intel generations); the working variant is used for the real encode and the reason for a failed test names both attempts.

## 0.218.5
- The image installs the non-free Intel media driver (needed to encode HEVC on recent Intel chips), vainfo and the Mesa VAAPI drivers for AMD (untested). INSTALL.md lists what is supported (Intel, AMD; NVIDIA not yet). The progress line and history name the method ("hardware" or "CPU: why not").

## 0.218.4
- Activity > Needs review: a 2160p last-resort candidate says "will be re-encoded to 1080p on accept".

## 0.218.3
- Search, more tolerant about the date: studios often date a release a day off from the provider. When the exact date finds nothing, the queries also try the day before and after (2 more requests for a scene that is not found). A release of the scene's own studio, one day off, whose name holds nothing the title / performers do not explain, is offered under Needs review (never grabbed automatically). For an older scene the review may offer a 2160p / SD release.
- The review rule "names every credited performer" uses the performers' own names again (an alias can be another person's stage name).

## 0.218.2
- Search: a release that names the performer by the credit the scene gives them ("as Kitty Wild" for the performer "Xenia Blondi") or by a full-name alias is recognised now (together with the right date). Short single-word aliases are not used.

## 0.218.1
- A release that was rejected or blocklisted for one scene no longer stays refused for every other scene: the duplicate guard refuses a name that is in the client's history or in the program's own list of downloaded releases only when that release was imported (it is in the library). The list of removed history entries records how each entry ended (imported / rejected).

## 0.218.0
- Last resort for older scenes: when nothing in the quality profile exists for a scene that is at least 10 days old (setting, 0 = off), a 2160p release is taken and scaled down to 1080p HEVC before the import (ffmpeg, quality target = variable bitrate, default 24; hardware encoding through /dev/dri when it works, else the CPU; progress on Activity). An SD release is taken as it is (switch). A failed encode imports nothing and leaves the download in place. A re-encoded file has new fingerprints, so it is finalized without the fingerprint comparison. Not for VR and not for upgrades. Settings > Quality > "Last resort for older scenes"; the Docker image includes the Intel VAAPI driver (INSTALL.md: add /dev/dri to the compose file).

## 0.217.1
- Search: two gaps found by running the wishlist through Whisparr's own search as a dry run: (1) the search stopped widening its queries at the first plausible-looking result even when that one was blocklisted or taken for another scene -- it now only stops at a result that could really be taken; (2) a studio called "Family Strokes: The Series" is released as "FamilyStrokes.26.10.16...", so the studio+date query is also tried with the name before the ":" (or " - ") subtitle.
- The "Whisparr" search button is gone under the default "bookkeeping only" setting: Whisparr's indexers have search switched off there, so the button found nothing. It stays for the settings in which Whisparr searches.

## 0.217.0
- Default view for the Releases page: next to the filter presets dropdown a star button makes the selected preset the default -- the page (List and Calendar) then opens with that filter already applied (e.g. Non-VR + StashDB only). Click the star again to remove it.

## 0.216.1
- Bulk dismiss buttons count scenes, not release rows ("Dismiss 10 on this page" next to "Dismiss all 9 matching" was a scene with two rows).

## 0.216.0
- Dismiss streak per studio: after 10 dismisses of one (sub-)studio in a row without putting one of that studio on the wishlist in between, the Dismiss card offers to hide the studio instead. Without a yes the next offer comes after 10 more dismisses. Single dismisses of new/backlog releases that came in through a studio count; "Dismiss N shown", wishlist dismisses and performer hits do not. A wishlist add or import of the studio starts the count again; Undo takes one back.

## 0.215.2
- Calendar view: "Hide studio" (eye icon next to the dismiss cross on a pill) for a release that came in through a watched studio. Asks first, has Undo; same action as the button in the List view.

## 0.215.1
- Cleaning the client's history also removes the entries of downloads this program rejected itself (blocklisted release names, quality-rejected downloads, other encodes of a blocked release) and of imported movies whose stored title ends in the NZB's first file name (".par2"). An entry marked as someone else's download by the post-processing script is never removed.

## 0.215.0
- Like Sonarr / Radarr / Whisparr ("Remove Completed"): once a download is imported for good, its entry is removed from SABnzbd's history -- only entries this program downloaded, never other programs' downloads, files untouched. Runs after every file-import check; per client switch "Remove completed downloads from the history" (default on) in Settings > Download Clients; "Clean history" per SABnzbd client does the same for everything already there (counts first, asks before).
- The release name of every removed entry is kept in the program's own records (part of the export), and the duplicate guard and the "Wrong download" blocklist read it, so a release is never fetched twice because the history was cleaned.

## 0.214.3
- The search button group (Now, Whisparr, Last, Manual) starts with a magnifier, so it is clear that all four buttons are searches.

## 0.214.2
- The movie button "Dismiss & delete group" is called "Dismiss & Delete" and has the same muted red as Dismiss.

## 0.214.1
- Activity > Stuck: movie rows (wanted, nothing found) have Dismiss, and "Dismiss & Delete" when the movie came from an empty Stash group -- the same buttons as on the Movies page, now one shared function.

## 0.214.0
- Stuck / On Wishlist rows: the search buttons are one joined group -- Now (new: the automatic search for this one release, right away, whatever the back-off says), Whisparr, Last, Manual -- and Dismiss sits apart from it. The same button names appear wherever the manual search is offered.

## 0.213.11
- The plain Dismiss buttons are a muted red, so they are not mistaken for the neighbouring buttons (calm on the dark theme too).

## 0.213.10
- The header health light compares the sidecar version with the plugin version: on a mismatch the dot turns amber and the tooltip says to rebuild the sidecar or update the plugin.

## 0.213.9
- The green health light in the page header shows the sidecar version ("Sidecar v0.213.9").

## 0.213.8
- Wishlist rows that came in through a studio now show the studio with its icon and the performers beside it without an icon, each linked to Stash (mirror of a performer row: performer with icon, studio beside it).

## 0.213.7
- Activity > Stuck and Releases > On Wishlist: the performers of a scene are shown next to the studio ("with A, B, ... +N"); read once per scene from the provider and stored.

## 0.213.6
- Search: a release is also accepted when its name carries the scene's exact date and at least two of the title's significant words and names nothing foreign to the scene (studio compilations, whose releases name none of the performers).

## 0.213.5
- Activity > Stuck and Releases > On Wishlist: a "Dismiss" button on wishlist releases nothing was grabbed for, with Undo; a release that is monitored in Whisparr is unmonitored there too.

## 0.213.4
- RSS search mode: a wishlist scene dated further back than the RSS feed reaches (the provider added it days after the release) now gets one targeted indexer search in the next run instead of after a full day (at most 10 per run).

## 0.213.3
- New: "Restart sidecar" in Settings > General > Backup & move and after an import, so no command line is needed. The container ends its process and Docker starts it again (compose `restart: unless-stopped`); the page waits until it answers again.

## 0.213.2
- Moving to another machine: the import is no longer refused because the new installation's watchlist is not empty (the setup guide fills it from Stash by itself and the import replaces it anyway). Only releases and movies still make an installation "not empty". INSTALL.md says to choose "Later" in the setup guide's last step.

## 0.213.1
- Settings > Connections (Prowlarr, Whisparr, StashDB, ThePornDB): URL and API key are in one form with one Save; "Check connection" saves what you typed first. The check says "no address saved yet" when no URL is saved.

## 0.213.0
- New: Maintenance > Movies > Compare groups. Film folders without a group are shown next to the empty groups they may belong to (the same film under a slightly different name: "Vol. 19" / "19", "The Perfectionist" / "Perfectionist, The", "Lesson 20", accents, brackets, roman numerals). The numbers in the names must be the same; the year is only a hint. "Add to that group" puts the video(s) in that group and opens it, "Not this" hides that pair for good (with Undo). A second list shows the empty groups that no folder matches. Nothing changes without a click.

## 0.212.5
- Movies & groups > Scrape…: a page the scraper cannot read right away (some sites refuse a request now and then) is tried up to three times, and a final failure is shown as a readable message instead of "HTTP 500".

## 0.212.4
- Movies & groups > Scrape… > Page address: the confirmation no longer shows the pasted address as the group's name; it says the name and data come from the scraper.

## 0.212.3
- Movies & groups > Videos without a group: when a group of the same name already exists (it can be another film), the row also offers "Create new group" and "Scrape…" next to "Add to that group".

## 0.212.2
- Maintenance > Upgrade wanted: a file whose scene has no StashDB / ThePornDB id no longer shows Dry run / Search now / Search…; the row says to identify the scene first and links to Maintenance > Unidentified scenes.

## 0.212.1
- Movies & groups: the group created by "Create group" opens in a new tab reliably (the tab is opened inside the click, so the browser does not block it).

## 0.212.0
- Maintenance > Movies & groups > Videos without a group: after "Create group" the row stays and shows "Open group", so scrape or identify can be done right away.
- New per row: "Scrape…", built like Stash's Edit Group page. The page is read by whichever group scraper installed in Stash handles its address. Way to a page: a title search at a source (AdultDVDEmpire today; Stash's group scrapers only read a page, so the search is done by the sidecar and more sources can be added) or a pasted page address. "Preview" opens the result's page in a new tab; "Create group" lets Stash's scraper read it and creates the group with its data (name, date, length, director, synopsis, studio and tags that exist in Stash, covers) and puts the scene(s) in it.

## 0.211.6
- Search: a release name that says SD without "480p" (an "SD" segment, or the SD variant of a release group) now counts as 480p, so it no longer lands in Activity > Needs review when the quality profile does not allow SD.

## 0.211.5
- Live updates (and the export upload) also work when Stash is opened over HTTPS behind a reverse proxy: the plugin then opens the socket on the page's own address (wss://<stash host>/events/ws) instead of the blocked ws:// one. INSTALL.md describes the proxy setup for Nginx Proxy Manager (with and without Authelia) and plain nginx. (Contributed in pull request #1.)

## 0.211.4
- Maintenance > Duration mismatch: lengths are shown as [h:]mm:ss, the badge shows the difference with the percentage in brackets, and a provider length under 10 seconds counts as no length. (Pull request #1.)

## 0.211.3
- Requests to Prowlarr wait up to 30 seconds (was 15). (Pull request #1.)

## 0.211.2
- A scene you already dismissed stays dismissed when another watched entry (performer, studio, parent studio) reports it later; before, it could show up in the Calendar again.

## 0.211.1
- Activity: movies that are wanted or failed get the "Search…" button (own search term + Grab), like scenes and the Movies page.

## 0.211.0
- Search page, By tag: tags can be excluded. Click a tag once = scenes must have it, twice = scenes must not have it, three times = off. Presets keep the exclusions.

## 0.210.0
- Search page, By tag: select several tags at once, a scene must carry all of them. The selection stays while you search for more tags.
- Tag combinations can be saved as presets (per provider).

## 0.209.3
- Dismiss (Calendar, List, late additions): a failed request now shows "Dismiss failed" instead of failing silently.

## 0.209.2
- Inline questions ("Add to wishlist? Yes / Cancel"): when the question does not fit on one line the text wraps and the two buttons stay together.

## 0.209.1
- Search page: on a scene marked "In library" the cover and the title open the scene in Stash; the separate button is gone.

## 0.209.0
- Search page: scenes that are already in Stash get "Re-add" and "Re-add + search". The scene goes back on the wishlist; after a verified download the new file replaces the old one (the old file goes to the recycle bin).
- Maintenance > Duration mismatch: lists library scenes whose file length differs from the provider's length by more than a threshold (default 5 %, shorter or longer), with Re-add per scene. Scenes without a provider length are not checked.

## 0.208.1
- Releases list: a release that came in through a watched studio but belongs to one of its sub-studios now shows the scene's own studio next to the watched one, so "Hide studio" is clearly about the sub-studio.

## 0.208.0
- Search page, ThePornDB: a "Movies" tab searches ThePornDB movies and puts one on the film wishlist ("Add to film wishlist"); a movie already there shows "on film wishlist".
- Search page: the tag search is now "By title | By tag" under Scenes and only offered for providers that can do it (StashDB and added stash-boxes, not ThePornDB).

## 0.207.2
- Search page: a scene marked "In library" has an "Open in Stash" button.

## 0.207.1
- Search page: a scene that Stash already has (a file under the same provider id) is marked "In library" and cannot be added again, also when this program never saw it before. If Stash does not answer, the search works as before.

## 0.207.0
- Search page: new way to search scenes by tag (StashDB and added stash-boxes): the term finds tags, a click lists the tag's newest scenes with Add / Add + search. ThePornDB cannot do it (its tag filter returns nothing).
- Search page: the image and title of every card open the item's page on the provider's website.

## 0.206.1
- Search page: the plugin waits up to 60 seconds (was 20) for a search, so a slow stash-box no longer ends in "Read timed out" while the sidecar is fine.

## 0.206.0
- Search page, scenes: a scene this program already knows is shown as such, also on a new search. On the wishlist: purple with "On Wishlist", "Search now" and "Dismiss"; imported: green; import failed: red; dismissed: grey with "Restore". After "Add" / "Add + search" the card changes at once.

## 0.205.6
- Search page: the cards of a page have the same height; scene images are 3:2, performers 2:3, studio logos sit whole in a square box. Missing images are a grey box of the same size, the buttons sit at the bottom of the card, long words wrap.

## 0.205.1
- Fix: "Possible match" on the wishlist no longer offers a release in a resolution the quality profile does not accept (e.g. 2160p while the profile takes 1080p / 720p).

## 0.205.0
- Two ways to put a release on the wishlist: "Add" only adds it (the scheduled search finds it later), "Add + search" also asks the indexers right away. A slow indexer no longer ends in a time-out error: the search runs in the background after 20 seconds, the result is in Activity / History, and a second click does not start another search.

## 0.204.3
- Compare / Review "Search via Stash scraper": ThePornDB is offered as Stash's stash-box (the same source as "Scrape with ..." in Stash) instead of the community scraper of that name, which failed on systems that cannot verify the certificate chain of its server.

## 0.204.2
- Maintenance: the number behind "Review: Identify Scene" (and the other links) follows the list while scenes are worked off.

## 0.204.1
- Review: Identify Scene: opening a single scene showed the word "null" above the buttons at the bottom. Fixed. A timeout of Stash's scraper now says that Stash itself is reachable and suggests pasting the scene's page address instead of a title search.

## 0.204.0
- Setup without copying, contributed by AnonTester: the plugin's only setting is the Sidecar API URL. The Stash API key and the StashDB / ThePornDB keys are read from Stash itself (the keys never pass the browser); the Stash URL is prefilled from the browser's address and confirmed in the setup guide. Other stash-boxes set up in Stash (FansDB, JAVStash ...) are offered as providers and created on request. "Update keys from Stash" overwrites typed keys on purpose.
- The download client form has a "Test connection" button that checks the details as typed (qBittorrent or SABnzbd) before saving; the old check no longer assumes SABnzbd.

## 0.203.1
- Fix: the setup guide's "Finish" step showed the word "null". New FIRST_STEPS.md with screenshots (documentation).

## 0.203.0
- Organized hook: scenes with a provider id in the Whisparr library folder (scenes/) are handled too, planned and moved like Maintenance > Organize library does for one scene.

## 0.202.0
- Maintenance > Organize library checks existing films against the movie templates (folder, file name, year, capitals, edition notes such as "(DVD + Blu-ray Combo)") and offers to fix the differences. The warning about other renamers only appears while the Organized hook is on and another renamer is enabled.

## 0.201.0
- Activity: "Search all stuck now" sends one targeted indexer query for every stuck scene, after asking.

## 0.200.0
- Adding a performer or studio to the watchlist can load its catalog (setting: off / ask / automatic); the catalog works for every stash-box (FansDB, JAVStash ...), not only StashDB. Each stash-box has its own query delay and favorites interval (default: StashDB's); favorites of other boxes are synced too.
- Download clients: "Take over from Prowlarr" prefills name, type and address from Prowlarr's own client.

## 0.199.0
- New metadata providers get different colours. Search page: wrapped scene cards no longer spill text into the neighbouring pill.

## 0.198.0
- The Organized hook has its own section with a switch (off by default) and a mode (preview only / move for real).

## 0.197.0
- Safety check for moving and renaming (Settings > Import & Naming > Safety check): finds another plugin that renames files on scene updates (renamerOnUpdate and relatives) and then refuses to move files unless you confirm; also checks that target folders are inside a Stash library, that the formats cannot give every file one name, the Whisparr scope, and whether moved files were moved again by something else.

## 0.196.0
- Settings > Import & Naming > Naming: the movie part is headed "Movies/Groups".

## 0.194.0
- The movie layout is a template (Settings > Import & Naming > Naming > Movies/Groups): folder and file format with the tokens {Movie Title}, {Movie Year}, {Part Number}, {Studio Title}, {Quality Full}, {Source}, {Resolution}. The defaults give the layout of earlier versions.

## 0.193.0
- Modular naming: optional parts `< >`, replace-words rules (text / word / regex), tag folders (you choose which tags get a folder; the list order is the priority), tokens {Release Year}, {Source}, {Resolution}, {Tag Folder}, and a live example under the format fields.

## 0.192.0
- File and folder names stay within the file system's 255-byte limit: only the title is shortened.

## 0.191.0
- "Organized" hook: marking a scene Organized in Stash can move/rename it (off / preview / on, default off).

## 0.190.0
- Import folder: grouped movie scenes (one Stash group with a ThePornDB movie link, or marked Organized) are placed into the film layout.

## 0.189.0
- A download rejected for its file (too small, unreadable) blocks only that release name, not its other encodes.

## 0.188.0
- An open search panel is no longer closed by live updates of the list.

## 0.187.0
- Prowlarr text searches no longer filter by category on the server (some indexers file results under sub-categories); the XXX range is filtered locally.

## 0.186.0
- "Add to Wishlist" no longer fails when Whisparr does not know a very new scene (the sidecar searches on its own).

## 0.185.0
- The top-bar entry is one icon among Stash's utility icons. With the separate [Plugin Menu](https://github.com/northwindlab/stash-plugin-menu) plugin installed, this plugin registers itself in that menu (with the activity dot); without it a plain icon is drawn.

## 0.183.0
- Missing from Catalog: filter by co-performer (text field with a list of the other performers in the scenes).

## 0.182.0
- Missing from Catalog: scene pairing filter (B/G, G/G, B/G/G ...) and a pairing badge per row.

## 0.181.0
- Review > Identify Scene: "Move to _UNMATCHED" also removes the review tag, so the scene leaves the list at once.

## 0.180.0
- Backup & move: upload an export file from the browser (drop area), over a WebSocket.

## 0.179.0
- First-start setup guide: walks through the connections, search, download client, quality and content once.

## 0.178.0
- `SRM_DEV_MODE=1`: a development copy that runs no background jobs.

## 0.177.0
- INSTALL.md and a generic compose example; quiet start of a fresh installation.

## 0.176.0
- No search when the provider lookup fails.

## 0.173.0 - 0.175.0
- Backup & move (export / import of watchlist, releases, movies, decisions, settings); fixes in the release identity check used by the blocklist.

## 0.165.0 - 0.172.0
- Per-page dismiss, scene-import readability check, Whisparr status on movie rows, "Dismiss & delete group", Maintenance lists, "Unidentified scenes" page with StashDB fingerprint check, Review fixes.

Earlier versions: the foundation (watchlist, scan, releases, calendar, search, hand-off, import, maintenance pages).
