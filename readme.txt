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
  switching tabs or navigating views to avoid background network/CPU waste.
- Streaming providers and torrent resolution must run asynchronously with clean
  timeouts and failover mechanisms.

--------------------------------------------------------------------------------
9. TESTING & VERIFICATION
--------------------------------------------------------------------------------
- When modifying category menus, discover pages, or catalog logic, always run:
    python3 -m unittest test_category_menus.py test_continue_watching.py test_collections.py
- Benchmark data preparation and model construction times when touching menu
  logic to guarantee operations complete in <5ms.
================================================================================
