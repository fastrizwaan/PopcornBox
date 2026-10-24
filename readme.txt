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
   Activated when the user clicks "See All" on any discover catalog or Continue Watching.
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
- PopcornBox must ALWAYS be buttery smooth, fluid, and instantly responsive.
- ZERO UI freezes, ZERO hangs, ZERO lag, and ZERO CPU spikes.
- The GTK main thread must remain free to process events and render at 60+ FPS.
- Users often have 60+ addons installed, resulting in hundreds of catalogs and
  thousands of potential items. Never assume a small dataset.

--------------------------------------------------------------------------------
5. UI THREAD HYGIENE & ASYNCHRONOUS WORK
--------------------------------------------------------------------------------
- NEVER perform network requests, database queries, disk I/O, or heavy data
  processing on the GTK main thread.
- ALWAYS offload data fetching, API communication, and manifest parsing to
  background daemon threads (`threading.Thread(..., daemon=True)`).
- Use `GLib.idle_add()` ONLY to deliver prepared results to the UI in small,
  non-blocking chunks. Do not run heavy loops or computations inside idle callbacks.

--------------------------------------------------------------------------------
6. MENU BUTTONS & GTK MENU MODELS
--------------------------------------------------------------------------------
- DO NOT POPULATE MENUS ON CLICK:
  Attaching or rebuilding a `Gio.Menu` or `GtkPopoverMenu` on click (e.g., inside
  a `notify::active` callback) causes GTK to synchronously instantiate widgets,
  parse CSS styling, and recalculate layouts during the click event. This results
  in an annoying lag, visible freeze, and high CPU spike right when the user clicks.

- PRE-ATTACH MODELS AHEAD OF TIME:
  Construct `Gio.Menu` models in a background worker thread. Once built, attach
  them to `GtkMenuButton` widgets via `GLib.idle_add()` across idle slices BEFORE
  the user interacts with them. When the user clicks the menu button, the menu
  is already built and attached, opening INSTANTLY on the first click.

- PREVENT MENU BLOAT (KEEP MENU HIERARCHIES SHALLOW):
  - Do NOT generate deeply nested submenus with hundreds or thousands of leaves
    (e.g., nesting 20-50 genres under every single catalog in header menus).
  - PopcornBox already provides dedicated header dropdowns (e.g., `genre_dropdown`)
    in the content grid view. Category menus (Movies, Series, Anime) must only
    provide direct navigation: Addon -> Catalogs (e.g., Popular, Top, New).
  - Streamlining menus from ~1,400 items down to ~200 items reduces widget
    instantiation overhead by 85-95% and cuts menu build time to <1ms.

- HANDLE ACTIVE STATE SAFELY:
  If a background update finishes while a user currently has a menu popover open,
  do NOT replace the model immediately (which tears down the popover). Defer
  the update until `notify::active` indicates the popover has closed.

--------------------------------------------------------------------------------
7. FAST STARTUP & INSTANT TAB SWITCHING
--------------------------------------------------------------------------------
- FAST LAUNCH:
  Startup must take milliseconds. During window initialization, assign lightweight
  placeholder models (`★ All Catalogs`) immediately, and defer full menu builds
  using `GLib.timeout_add(500, self._ensure_all_menus_built)`.
- INSTANT TAB SWITCHING:
  Switching between linked buttons (Discover, Movies, Series, Anime) must be
  instantaneous. Tab switch handlers (`on_switch_movies`, etc.) must simply change
  stack pages and trigger discover page refreshes; they must never synchronously
  construct or re-attach menu models.

--------------------------------------------------------------------------------
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
- DON'T SHRINK THE WINDOW TO "FIX" MISSING CARDS. A serial window was measured
  against a real addon (1.2 s responses) and it dropped 3 of 6 catalog rows: the
  polite queue outlived the discover loader's deadline. If a filter needs more
  results, fetch fewer but DEEPER pages (`_MAX_DISCOVER_PAGES`), cache them, and
  ask for the next page when the user scrolls - do not widen concurrency.
- CARD METADATA IS ASKED FOR ONCE, EVER. `api.METADATA_CACHE_HOURS` is `None`:
  discover pages, meta responses, poster lookups and TMDb id resolutions, plus the
  `catalog_cache` rows they build (`database.CATALOG_CACHE_HOURS`), never expire,
  so a card the user has already seen costs the provider nothing a second time.
  Poster bytes and `metadata_cache` rows were always permanent; this makes the
  JSON that points at them permanent too. Everything that genuinely changes keeps
  an explicit TTL: streams and subtitles 24 h, IPTV playlists 24 h, searches 2 h,
  the addon directory 12 h, manifests a week. `api._prune_disk_cache_if_needed()`
  therefore no longer deletes files for being old - freshness is the reader's
  decision - and only trims past `_DISK_CACHE_MAX_BYTES`, least recently read
  first. Never shorten a metadata TTL to make a grid feel fresher, and never make
  the pruner age-based again: that is how the same posters get re-requested.
- A HOST THAT SAYS "SLOW DOWN" IS BELIEVED: HTTP 429 with `Retry-After`, and
  provider rate-limit notices, set a cooldown via `api.set_provider_cooldown()`.
  While a host cools, the app serves the disk cache instead of requesting, and
  cache TTLs are never cut to dodge it or to squeeze out more cards.
- WAITING IS FOR BACKGROUND THREADS ONLY. The GTK main thread never sleeps for
  pacing: it makes one non-blocking attempt and is answered from cache (or with
  nothing). Every fetch belongs on a daemon thread, so reaching that path is a
  bug in the caller, not a policy to widen.
- DEPTH OVER BREADTH WHEN A GRID RUNS DRY. `tmdb_helper._MAX_DISCOVER_PAGES` (20)
  is how deep one filter browses and each discover row renders 36 cards, so a grid
  reaches roughly 700 cards before TMDb says the filter is over. That costs two
  paced requests per page, cached forever - measured live at 423 cards over 30
  requests with a 250 ms gap between starts. When someone reports "fetching
  stopped", raise this depth; do not raise concurrency to get there faster.
- CACHED FIRST, NETWORK AFTER. Read the cache before the network and never ask for
  a page already held fresh. `fetch_country_catalog_page()` probes every URL with
  `cache_only=True` and fetches only the gaps, so re-browsing a filter costs zero
  requests. The Unique loader submits the local sources first and appends a
  network slice behind the cached slices it is waiting on, so the grid fills from
  what the machine already has. Inside one row the order stays TMDb's popularity
  sort - cached-before-network is about when results reach the UI, not a ranking.
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
- When modifying category menus, discover pages, or catalog logic, always run:
    python3 -m unittest test_category_menus.py test_continue_watching.py test_collections.py
- Benchmark data preparation and model construction times when touching menu
  logic to guarantee operations complete in <5ms.
================================================================================
