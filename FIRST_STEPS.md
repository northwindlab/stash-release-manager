# StashReleaseManager -- first steps

You have installed the sidecar and the plugin ([INSTALL.md](INSTALL.md)) and entered the basics in the setup guide. This page walks you from an empty installation to the first download, in the order you will meet things. It takes about ten minutes of clicking plus the time the first scan needs.

> The screenshots show placeholder names and grey boxes instead of real scene titles, performers and images. The numbered red marks are explained in the text next to each picture.

## 1. Where everything is

Open **Release Manager** from the box icon in Stash's top bar (next to *Statistics*) or from the plugin drop-down.

![Releases page](docs/img/01-releases-calendar.jpg)

1. The pages of the plugin: **Releases** (what is new and what is on your wishlist), **Activity** (what is being downloaded, stuck or needs a decision), **History** (a log of everything the plugin did), **Search** (find a performer, studio or scene), **Watchlist**, **Maintenance** (library tools), **Settings**.
2. **Run scan now** asks StashDB / ThePornDB for new releases of everyone you follow. A scan also runs by itself every few hours.
3. The sections of the Releases page: **New releases** (found by a scan, not decided yet), **On Wishlist** (the plugin is looking for them), **Imported**, **Dismissed** and **Added late** (older scenes that turned up in a catalogue afterwards).
4. **List** or **Calendar** view of the same data.
5. On every release: the arrow **adds it to your wishlist**, the **×** dismisses it, the person / camera icon opens the performer or studio in Stash.
6. The two dots show whether **Stash** and the **sidecar** can be reached. Both must be green. If one is not, see "If something does not work" in [INSTALL.md](INSTALL.md).

## 2. Check the setup guide

*Settings > General > Open the setup guide* shows what is entered and what is still missing. Every required step carries a tick when it is done.

![Setup guide](docs/img/06-setup-guide.jpg)

1. The steps: **Stash**, **StashDB**, ThePornDB (optional), **Search**, **Downloads**, **Quality**, Content & watchlist, Whisparr (optional), **Browser userscript** (optional: buttons on StashDB's pages, installed from the sidecar's address, see [INSTALL.md](INSTALL.md)), **Finish**.
2. Each step has the same fields as the Settings pages. Enter the **URL** of the service ...
3. ... and its **API key**, then press **Save** next to the field. Keys are stored in the sidecar's database and are never part of an export.

The same fields live under *Settings > Connections*:

![StashDB connection](docs/img/04-settings-stashdb.jpg)

1. The settings pages: **General**, **Connections**, **Search & Quality**, **Import & Naming**, **Jobs**.
2. Address of the service (the default for StashDB is fine).
3. The **API key** -- on StashDB it is on your user page.
4. Optional: **Sync now** copies your own StashDB favorites into Stash's Favorite flag (one direction, nothing is ever removed).

### The download client

The sidecar needs one download client (SABnzbd for usenet; qBittorrent is supported but less tested). Add it under *Settings > Connections > Download Clients*:

![Download client](docs/img/05-settings-download-client.jpg)

1. If you use **Prowlarr** and it already has a download client, **Take over from Prowlarr** fills name, type and address for you (*Prefill*). Prowlarr never reveals the API key, so ...
2. ... check the **URL** (as the sidecar reaches the client) ...
3. ... and type the **API key** yourself.
4. **Add client**, then **Check connection**.

Then set the **search backend** (Prowlarr, or your own Newznab indexers) and a **quality profile** under *Settings > Search & Quality*, and the two download folders under *Import & Naming > File Import* (see [INSTALL.md](INSTALL.md), step 5).

## 3. Choose who to follow

The plugin looks for releases of the **performers and studios on your watchlist**. There are three ways to put someone on it:

* In Stash itself: **star** a performer or studio (watchlist source *Favorites*) or give it the tag **Watchlist** (watchlist source *Tag*). Which of the two counts is set under *Settings > General > Watchlist source*. The watchlist is synced before every scan.
* On the plugin's **Search** page: find the performer or studio at StashDB or ThePornDB and press the button on its card.
* On the performer's or studio's own Stash page, with the eye icon the plugin adds there.

![Search](docs/img/02-search-watch.jpg)

1. Pick the metadata provider (StashDB, ThePornDB, and any stash-box you added under *Settings > Connections*).
2. Choose what to search for: **Performers**, **Studios** or **Scenes**.
3. Type a name ...
4. ... and press **Search**.
5. **Watch** (or **Favorite**, depending on the watchlist source) puts it on the watchlist. If it does not exist in your Stash yet, it is created there.

The **Watchlist** page shows everyone you follow:

![Watchlist](docs/img/03-watchlist.jpg)

1. Where the list comes from (tag or favorites) -- change it in Settings.
2. **Auto → Wishlist**: every new release of that entry goes straight to the wishlist, without asking. Off by default.
3. **Window**: limits everything for that entry to a date range, e.g. only scenes from 2020 on.
4. Search and filter by studio / performer.
5. Jump to a letter.

**Tip:** under *Settings > General* the setting **Load the catalog of new entries** decides what happens with the *old* scenes of someone new: *Off* (you load them by hand per year), *Ask* (the Search page asks after you pressed Watch) or *Automatically* (the whole catalogue inside the date window is loaded in the background). The **default date window** next to it keeps that from loading twenty years of scenes.

## 4. The first scan

On the Releases page press **Run scan now** (mark 2 in the first picture). The first scan only notes the newest scene of everybody (the "baseline"); from then on every new scene shows up under **New releases**. A big watchlist takes a while: the progress is shown above the list.

Scenes that are older than the watchlist entry appear under **Added late**, or -- if you load them on purpose -- under **Missing from Catalog** (step 6).

## 5. From release to library

On a release in the list view there are two buttons: **Add** puts it on the wishlist and leaves the search to the schedule (Settings > Jobs); **Add + search** also asks the indexers right away. (In the calendar the arrow on a pill is *Add + search*.) A search that takes longer than about 20 seconds goes on in the background, so a slow indexer no longer ends in an error message; the result shows under Activity and History. The plugin then:

1. looks for the scene at your indexers (Prowlarr or direct), with the scene's current title, date and cast from StashDB;
2. picks the best match for your quality profile -- only a result that really is that scene; anything doubtful is shown for you to decide;
3. sends it to your download client;
4. when the download is complete, **moves it into your library** by the naming scheme, and tells Stash to scan it. The scene then shows as **Imported** and has the provider's title, date, studio and performers.

**Activity** shows what is going on:

![Activity](docs/img/07-activity-stuck.jpg)

1. **Needs review**: a result that might be right but is not certain -- you decide. **Downloading & importing**: in progress. **Stuck**: searched, nothing usable found yet. **Source file missing** and **Import failed**: something to look at.
2. **Search all stuck now**: one targeted indexer query for every stuck scene, once, right now (the normal schedule asks each scene daily in its first week, then weekly). It tells you how many queries it will send and asks first.
3. Each row has a button group: **Now** runs the automatic search for this scene again right away; **Whisparr** (only when Whisparr does the searching, not in the default setup where this program searches and Whisparr only keeps the books) hands it to Whisparr's own search; **Last** shows why a scene is stuck (no result, wrong title, quality ...); **Manual** lets you search by hand with your own words and pick a result yourself. **Dismiss** (red, set apart) takes the scene off the wishlist.

Nothing is downloaded twice: a scene that is already in your library, or already in the download client's history, is not fetched again.

## 6. Older scenes: "Missing from Catalog"

On a performer's or studio's **Stash page** the plugin adds a **Releases** tab:

![Releases tab on a studio page](docs/img/08-studio-releases.jpg)

1. The **Releases** tab (next to Scenes, Galleries, ...).
2. **New releases** for this entry, with *Add to Wishlist* (4) and *Dismiss* per scene.
3. **Missing from Catalog**: the complete catalogue at the provider, year by year, compared with what you own. Pick years to load, or set a date window; one panel per stash-box this entry is linked to.

## 7. The Maintenance page

Maintenance is for your **existing** library. Nothing in it changes a file until you click.

![Maintenance](docs/img/09-maintenance.jpg)

1. **Check library**: *Fingerprint check* (files whose Stash tag does not match the file, by fingerprint, against the provider you have set up -- StashDB, ThePornDB or another stash-box; with several there is a *Check against* drop-down; scenes the provider cannot judge wait under *Needs manual review*), *Performers without scenes*, *Unidentified scenes* (scenes without a provider id), *VR: 2D / mono files* (the check's doubts about VR files; you say what a file is).
2. **Resolve**: *Review: Identify Scene* (cases nobody could identify yet), *Compare scenes* (several versions of one scene).
3. **Files & quality**: *Import folder* (files that were not downloaded here, e.g. an old collection), *Organize library*, *Upgrade wanted* and *Convert* (files no browser plays directly -- old video codecs such as MPEG-4 in AVI, WMV, FLV, MPEG-1 -- are turned into H.264 + AAC in an MP4 by an encode worker, see [INSTALL.md](INSTALL.md); the scene stays, the old file goes to the recycle bin).
4. **Organize library** has two tabs: *Scenes by studio* (**Recount** checks every studio against StashDB once, in the background; **Details...** shows what would be renamed or moved, **Reload** checks it again; renaming returns at once and Stash scans in the background) and *Films vs. movie templates* (*Check films*).
5. **Upgrade wanted** lists files that should be replaced by a better version: VR files that are 2D / mono / low bitrate, files below the quality profile's cutoff, and files whose **length differs** from the provider's (cut or partial rips; choose the reason *wrong length* for the threshold, shorter / longer and the button *Run duration check*). Candidates found by a check wait in *To check*; nothing is searched until you release it (*Queue for search* or *To wishlist*).
6. **Movies & groups**: films and Stash groups, with the tabs *Videos without a group*, *Compare groups* (every group without a file: matched with a video, a duplicate of a group with files, or without a suggestion) and *Already in the library*; *Duration: movie scenes* is its own entry (scenes of films are not upgrade candidates).

The numbers behind the entries of the menu are what waits for your decision. The results of a check are **kept** (in the sidecar or in your browser) until a change takes an entry out of them: opening a page again does not repeat the work, and the button **Reload** (circular arrow) asks again.

Before the first rename or move, look at *Settings > Import & Naming > Safety check*: another plugin that renames files (renamerOnUpdate ...) must not be active at the same time as the **Organized hook**.

## What is switched off until you switch it on

* The **Organized hook** (move / rename a scene when you mark it *Organized* in Stash): *Settings > Import & Naming > Organized hook*. Start with *Preview only*.
* **Auto → Wishlist** per performer / studio, and the **upgrade search**.
* **Whisparr** is optional; without it the plugin does everything itself.

## If something does not work

* Releases do not appear: *Run scan now*, then look at **History** (it says what each job did or why it did nothing).
* A scene stays on the wishlist: **Activity > Stuck > Last search**, and the quality profile under *Settings > Search & Quality*.
* The two dots in the header are not green, the page stays empty, a mount is empty: the table at the end of [INSTALL.md](INSTALL.md).
