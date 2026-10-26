================================================================================
POPCORNBOX - AI AGENT ARCHITECTURE, WORKFLOW & PERFORMANCE MANUAL
================================================================================

This document provides mandatory architecture details, repository layout, build
workflows, design patterns, and performance standards for AI coding assistants
working on the PopcornBox codebase. Read this first to avoid token-heavy scans.

--------------------------------------------------------------------------------
1. PROJECT OVERVIEW & ARCHITECTURE
--------------------------------------------------------------------------------
PopcornBox is a modern, responsive streaming client built with GTK4 and Libadwaita
(Python). It aggregates media (Movies, Series, Anime) via Stremio addons, provides
bit-torrent streaming via libtorrent, and handles high-performance video playback
via MPV (python-mpv).

Key Source Files (`src/`):
- `src/window.py`:
  Core application window (`CineWindow`). Manages view stacks, navigation routing,
  Discover catalog rows, Continue Watching carousels, category menus, search,
  and playback transitions.
- `src/window.blp` & `src/window.ui`:
  Main UI layout declared in Blueprint (`.blp`) and compiled to GtkBuilder XML (`.ui`).
  Contains headerbars, search bars, `library_stack`, details views, and controls.
- `src/style.css`:
  Global GTK4 CSS stylesheets (dark theme, cards, headers, buttons, player OSD).
- `src/api.py`:
  Stremio protocol client: fetches addon manifests, queries catalog items, resolves
  media streams, and queries subtitle providers asynchronously.
- `src/database.py`:
  SQLite storage for user state: history, watched status, continue watching queue,
  favorites, downloaded items, cached catalogs, working streams, and addon states.
- `src/movie_widget.py`:
  Item cards: `MovieWidget` (standard posters) and `ContinueWatchingWidget` (progress
  bars, quick play button, remove button). Handles async cover loading and caching.
- `src/player.py`:
  MPV player integration (`python-mpv`). Manages playback, subtitle tracks, audio
  tracks, chapter navigation, buffering states, and fullscreen OSD overlays.
- `src/libtorrent_stream.py`:
  BitTorrent sequential streaming engine: peer exchange, piece prioritizing,
  smart buffer management, and automated disk cleanup.
- `src/preferences.py` & `src/preferences.blp`:
  Preferences dialog for addon management, scrapers, players, and download dirs.
- `src/tmdb_helper.py`:
  TMDB metadata enrichment (posters, backdrops, episode descriptions, cast/crew).

--------------------------------------------------------------------------------
2. UI STRUCTURE & VIEW HIERARCHY
--------------------------------------------------------------------------------
`library_stack` (AdwViewStack) switches between primary library screens:
1. `"discover"`:
   Scrollable feed (`discover_box`) containing horizontal scrolling carousels:
   - "Continue Watching" row (top-most when active).
   - Addon catalog rows (Popular, Trending, Top, etc.) loaded lazily in batches.
2. `"content"`:
   Grid view (`content_flowbox`) with `discover_back_box` header (`< Section Title`).
   Activated by See All on regular provider catalogs or Continue Watching.
   Network See All opens the paginated TMDb network results in the full Discover grid.
3. `"search_results"`:
   Categorized search grids (Movies, Series, Anime).

UI Spacing & Layout Guidelines:
- Headerbar gap to first section: 12px. The first section header must have
  `margin-top: 0px` (`.discover-section-header:first-child`, `.first-section-header`).
- Within a section: ~8px between section label and cards (both Discover carousels and See All grid).
- Between sections: ~22px vertical distance between cards of Section N and the
  title of Section N+1 (fairly distanced, not cramped, not overly spacy).
- Card sizing: Posters have size request 130x240px (`.pt-card`). Row scroll
  containers have `min-height: 250px`. Padding in `.discover-row-box` (4px top,
  6px bottom) accommodates the 1.04x card hover scale without clipping.

Discover data sources and filters:
- Regular Popular, Featured, New, and addon-specific rows come from the selected
  Stremio catalog providers. Cinemeta is one provider and also supplies metadata
  by IMDb ID; it is not the source for every Discover catalog.
- TMDb supplies direct country/language-filtered results and direct Network
  results. Broadcast networks use TMDb's TV-network relation for series and the
  corresponding production-company relation for movies: TMDb movies do not use
  the TV-network field. Indian OTT services such as JioHotstar, ZEE5, SonyLIV,
  Sun NXT, aha, hoichoi, Lionsgate Play, ManoramaMAX, Chaupal, and Hungama Play
  use TMDb's regional watch-provider availability filters for India where those
  providers are listed. Provider names are resolved from TMDb's regional
  provider list rather than relying on stale numeric IDs. These availability
  catalog pages refresh daily. Some services also have TMDb TV network records:
  Amazon MX Player (network 7774), MX Player (network 2964), and Eros Now
  (network 2716) use those for series, while their movie catalogs use
  production-company IDs. Amazon MX Player and MX Player are separate entries.
  Chaupal falls back to its production-company relation when TMDb has no
  matching India watch-provider entry. This avoids treating an OTT service as
  a production studio or claiming a movie is available from its production
  company. The dropdown is curated to popular services and networks, rather than
  exposing every production studio.
- Network, media type, genre, year, original language, and country can be
  combined. These filters are passed to TMDb Discover when a TMDb Network or
  country/language catalog is active. When a language filter has a TMDb row,
  Unique uses that direct query (including a selected genre) instead of also
  loading the slower hydrated local genre-cache row. A type change keeps other
  selections if they are still offered; Reset returns dropdowns to their All
  values and turns off Unique.
- Title searches use TMDb's movie and TV search endpoints, with unified
  multi-search as a fallback when those endpoints return no usable cards.
  TMDb may return typo-tolerant title matches; Title Only must not remove those
  matches with a second exact-substring check.
- Network See All opens the full paginated results view. Unique uses the direct
  TMDb network feed when a network is selected, rather than scanning every addon
  catalog and waiting for its metadata to hydrate. Pages are fetched as the user
  scrolls; TMDb's 500-page API ceiling is the hard upper bound, not a number of
  cards preloaded into GTK at once.
- A TV series can have a network, a production studio, or both. Only the
  `networks` field is used to match the Network filter for TV; `production_companies`
  is retained as separate metadata and is not treated as a network alias.

--------------------------------------------------------------------------------
3. FLATPAK BUILD & DEVELOPMENT WORKFLOW
--------------------------------------------------------------------------------
Our primary build, test, and runtime target is Flatpak:
App ID: `io.github.fastrizwaan.PopcornBox`
Runtime: `org.gnome.Platform//50` | SDK: `org.gnome.Sdk//50`

- Host OS vs Flatpak Runtime:
  Native dependencies (such as `libmpv`, `libtorrent`, and `blueprint-compiler`)
  reside inside the Flatpak container. Running code directly on host Python may
  fail if host packages are absent.

- Building & Installing Flatpak:
  ```bash
  ./build_bundle.sh
  # OR directly via flatpak-builder:
  flatpak-builder --user --install --force-clean --disable-rofiles-fuse build-dir build-aux/flatpak/io.github.fastrizwaan.PopcornBox.json
  ```

- Running Flatpak:
  ```bash
  flatpak run io.github.fastrizwaan.PopcornBox
  ```

- Compiling Blueprint Files (`.blp` -> `.ui`):
  Whenever modifying `src/window.blp` or any `.blp` file, always recompile to
  the corresponding `.ui` XML file so both stay in sync:
  ```bash
  flatpak run --filesystem=$(pwd) --command=blueprint-compiler org.gnome.Sdk//50 compile src/window.blp --output src/window.ui
  ```

--------------------------------------------------------------------------------
4. CORE PERFORMANCE PHILOSOPHY
--------------------------------------------------------------------------------
- Prioritize responsive input and scrolling, especially while loading details,
  starting playback, changing pages, and opening menus.
- Keep main-thread work bounded so GTK can process events and render between
  updates. Set performance budgets from measurements on representative hardware;
  do not promise a fixed frame rate or zero CPU use for every workload.
- Test with large addon sets and libraries. Avoid assuming the visible dataset is
  small, but do not materialize every possible result as a widget at once.

--------------------------------------------------------------------------------
5. UI THREAD HYGIENE & ASYNCHRONOUS WORK
--------------------------------------------------------------------------------
- Keep the GTK main thread free of work that can wait: network requests, torrent
  shutdown or startup waits, disk writes, fsync, and unbounded parsing or loops.
  Playback clicks must show the loading/player state promptly, then continue setup
  without waiting for persistence or cleanup.
- Use background workers for network and blocking I/O. Prefer existing async
  database APIs; when several related values must be persisted, batch them into
  one locked write instead of issuing multiple full-file rewrites.
- Small, bounded reads from an in-memory cache or indexed local database can stay
  on the main thread only when they are known to be fast and do not contend with a
  long write. Move them off-thread if profiling shows lock waits or UI stalls.
- GTK widgets and view mutations belong on the GTK main thread. Workers should
  return plain data; apply it with `GLib.idle_add()` in bounded chunks. Idle
  callbacks still run on the UI thread, so they must not contain blocking I/O or
  large widget-building loops.
- Avoid starting duplicate work for the same item. Cancel or invalidate stale
  requests and coalesce repeated saves/fetches where possible.

6. MENU BUTTONS & GTK MENU MODELS
--------------------------------------------------------------------------------
- Keep menu models modest and avoid rebuilding them in a click/active callback.
  Reuse stable models when their contents have not changed.
- If menu contents depend on slow data, prepare plain menu-item data in a worker.
  Create and update `Gio.Menu`, GTK widgets, and popovers on the GTK main thread;
  do that work before the menu is opened and in bounded slices when the menu is
  large. Do not call GTK APIs from a worker thread.
- Keep menu hierarchies shallow and avoid repeating large sets of choices under
  every catalog. Put filters in the dedicated filter controls where practical.
  Measure the actual menu build and open time; do not rely on fixed item-count
  percentages or sub-millisecond timing claims.
- If a menu update completes while its popover is open, defer replacing its model
  until the popover closes so the active interaction is not torn down.

7. FAST STARTUP & RESPONSIVE NAVIGATION
--------------------------------------------------------------------------------
- Keep window construction limited to essential widgets and cached state. Start
  network refreshes and expensive derived work after the first frame, with clear
  cancellation when the user changes pages.
- Do not use a fixed delay as a substitute for measuring startup work. Schedule
  optional work when the main loop is idle, and keep each main-thread slice small
  enough to avoid visible stalls.
- Page switches should update the visible stack promptly. Reuse already-rendered
  pages where practical, and fetch or build only the data needed for the current
  page and its near-viewport buffer.

8. STREAM & IMAGE HANDLING
--------------------------------------------------------------------------------
- Cancel pending image downloads (`cancel_pending_image_downloads()`) whenever
  switching tabs or navigating views to avoid background network/CPU waste. That
  also drops the pending blank-tile repairs below: a rebuilt grid fetches its own
  pictures.
- A CARD WITH NO PICTURE IS A BUG, NOT AN END STATE. `movie_widget` keeps a repair
  queue of the tiles that ended up empty - the host's image window ran out of
  patience, the CDN timed out, the response could not be decoded - and one slow
  GLib tick re-issues them, a slice at a time, through the same polite window.
  Tiles with no poster URL at all are queued too: `fetch_fallback_poster()` notes
  the card when every lookup came back empty, and the tick re-runs that lookup, so
  a title the addons could not answer once gets asked again later. Because it is
  the same code path, a repair costs cached reads unless the provider really has
  something new. The attempt count lives on the tile, so a dead URL gets a bounded
  number of tries and then rests; a tile that filled itself, moved on to another
  picture or was discarded with its card simply leaves the queue. A re-ask only
  paints the tile while it still carries the same card's stamp, so a widget the
  flowbox recycled for another title is never painted with the old poster.
- FAILED IMAGE URLs EXPIRE. `FAILED_IMAGE_URLS` remembers the last failure per URL
  for `_FAILED_IMAGE_RETRY_SECONDS`, it is not a process-lifetime ban: one
  timeout during a fast scroll used to leave that poster blank until the app was
  restarted. A successful download clears the mark.
- CARDS PAUSE WHILE ANOTHER PAGE HAS THE SCREEN, AND FINISH ON THE WAY BACK. A tile
  that is attached to a window but not mapped - hidden tab, movie details, the
  player - does not start a download: no request, no decode, and the two image
  permits of its host stay free for the page the user is actually looking at. The
  tile records what it still owes and GTK's own `map` signal re-issues it when the
  page returns, so pressing Back finishes the grid by itself. This is per tile, not
  a global pause flag: no navigation path can forget to resume. (GTK4 emits `map`
  on that switch; `notify::mapped` does NOT fire - use `map`.) A card click still
  cancels in-flight downloads to keep the details page fast, and those tiles are
  then finished the same way, which is what used to leave "many blank cards whose
  images were not fetched" after a details visit. Tiles parked this way never spend
  one of their bounded repair tries. A widget that has no window root yet is a grid
  on its way to the screen and downloads immediately, exactly as before.
- When the hydrator resolves a poster for a card whose own URL never painted
  anything, `_apply_resolved_meta` repaints the tile - but only while it is still
  blank, so a card that already shows a picture is never flickered.
- Streaming providers and torrent resolution must run asynchronously with clean
  timeouts and failover mechanisms.

--------------------------------------------------------------------------------
9. GENTLE FETCHING (DO NOT HAMMER SERVERS)
--------------------------------------------------------------------------------
- EVERY REQUEST PACES ITS HOST. Catalog, metadata and manifest traffic goes
  through `api._acquire_catalog_window(host)` inside `_get_cached_request()`;
  posters and backdrops go through `api._acquire_image_window(host)` in
  `src/movie_widget.py`; streams go through `api._acquire_provider_slot(host)`.
  All three pace themselves on ONE per-host clock (`_PROVIDER_LAST_REQUEST_TIME`),
  so a host never sees the rates added together. Feature modules must never issue
  a bare `urlopen`.
- THE CATALOG WINDOW IS BOUNDED, NOT SERIAL: at most `_CATALOG_HOST_PERMITS` (3)
  requests in flight per host, starts at least `_CATALOG_MIN_INTERVAL` (250 ms)
  apart, queueing capped at `_CATALOG_SLOT_WAIT` (4 s). Streams stay stricter at
  one request per host with 400 ms spacing. Images get their own class: TWO
  posters per host at a time (`_IMAGE_HOST_PERMITS`), 60 ms spacing, and a
  generous 15 s queue because a narrow window makes the last tile of a page wait
  for the whole page. One permit was tried and measured - it serialised a page of
  posters to ~0.8 s each - so two is the settled size; change the number, do not
  remove the window. A poster that still misses its slot stays blank and is
  retried by the repair tick in `src/movie_widget.py` (section 8).
- TUNE PAGINATION AND CONCURRENCY SEPARATELY. A missing row can come from the
  provider page limit, request queue deadline, cancellation, or rendering cost.
  Diagnose which limit is reached before changing it. Fetch additional result
  pages as the user approaches the end of the visible results; do not increase
  page depth or concurrency blindly to make a grid feel fuller.
- SUCCESSFUL METADATA AND CATALOG ROWS ARE STORED PERMANENTLY, BUT MISSING FIELDS
  ARE RETRIED. `database.CATALOG_CACHE_HOURS` is `None`, and metadata is merged
  into the persistent SQLite `metadata_cache`. A missing title, poster, genre,
  year, description, language, or TV network remains eligible for hydration;
  missing records are found in both metadata and catalog caches. When the
  hydrator is first triggered during startup, it scans those records and queues
  them for a background retry. This is why a missing item is retried on each app
  restart instead of waiting a week for the next attempt. During a running
  session, incomplete cached metadata is eligible again after seven days. A
  complete record is not periodically refreshed. Cinemeta is tried first, with
  TMDb enrichment for missing genres, language, country, and network/company
  fields. TV network checks have their own `networks_checked_at` timestamp.
  Successful metadata and catalog rows remain permanent; request caches still
  have explicit freshness rules. Streams and subtitles are 24 h, IPTV playlists
  24 h, searches 2 h, addon directory 12 h, and manifests a week.
  `api._prune_disk_cache_if_needed()` only trims past `_DISK_CACHE_MAX_BYTES`,
  least recently read first; it does not delete cached metadata by age.
- A HOST THAT SAYS "SLOW DOWN" IS BELIEVED: HTTP 429 with `Retry-After`, and
  provider rate-limit notices, set a cooldown via `api.set_provider_cooldown()`.
  While a host cools, the app serves the disk cache instead of requesting, and
  cache TTLs are never cut to dodge it or to squeeze out more cards.
- WAITING IS FOR BACKGROUND THREADS ONLY. The GTK main thread never sleeps for
  pacing: it makes one non-blocking attempt and is answered from cache (or with
  nothing). Every fetch belongs on a daemon thread, so reaching that path is a
  bug in the caller, not a policy to widen.
- KEEP FETCHING AND RENDERING BOUNDED. `tmdb_helper._MAX_DISCOVER_PAGES` limits
  country/language Discover feeds; `_MAX_NETWORK_PAGES` allows network catalogs
  up to TMDb's 500-page API ceiling. These are upper bounds, not preload targets.
  Fetch the next page as the user approaches the end of the visible results.
  Keep widget creation near the viewport plus a small buffer, and measure
  scrolling, memory use, and time to first useful content before changing page
  size or concurrency.
- CACHED FIRST, NETWORK AFTER. Read the cache before the network and never ask for
  a page already held fresh. `fetch_country_catalog_page()` probes every URL with
  `cache_only=True` and fetches only the gaps, so re-browsing a filter costs zero
  requests. The Unique loader normally submits local sources first and appends a
  network slice behind cached slices it is waiting on. When a language-filtered
  TMDb row is available, it skips the synthetic local genre-cache row so slow
  metadata hydration does not delay the direct filtered results. Inside one row
  the order stays TMDb's popularity sort - cached-before-network is about when
  results reach the UI, not a ranking.
- SIZE FAN-OUT POOLS TO THE WINDOW where they are created (see the TMDb
  country/language fan-out in `src/tmdb_helper.py`), so workers queue on the
  window instead of piling up behind it, and submit the must-have URLs first so
  they get the early slots when a shed happens.
- BUDGET CHECK before adding any fetch: how many requests does one filter, tab
  switch or scroll make, per host? Stay inside the pacing rate, cache the result
  (forever for catalog pages and card metadata, 24 h for streams), propagate
  `is_cancelled` so an abandoned batch stops queuing, and verify against the live
  endpoint with recorded request times.

--------------------------------------------------------------------------------
10. TESTING & VERIFICATION
--------------------------------------------------------------------------------
- Run focused checks for the behavior changed, plus nearby regression coverage
  when available. Avoid running unrelated suites as a substitute for profiling.
- For responsiveness work, measure the user-visible interaction before and after:
  time to first frame, input-to-response delay, main-thread callback duration, and
  memory/widget count for representative large libraries. Record the dataset and
  machine used; use the results to set realistic budgets rather than fixed timing
  promises that do not account for hardware or workload.
